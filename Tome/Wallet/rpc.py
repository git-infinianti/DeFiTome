from decouple import config
from evrmore_rpc import EvrmoreClient

RPC = EvrmoreClient(datadir=config('RPC_DATADIR', default='/tmp/evrmore'))


def create_raw_transaction(inputs, outputs):
    """
    Create a raw transaction on the Evrmore network.
    
    Args:
        inputs (list): List of dicts with 'txid' and 'vout' keys
                       Example: [{"txid": "...", "vout": 0}]
        outputs (dict): Dict mapping addresses to amounts
                        Example: {"EVR_ADDRESS": 0.5}
    
    Returns:
        str: Raw transaction hex string
    
    Raises:
        Exception: If RPC call fails
    """
    try:
        raw_tx = RPC.createrawtransaction(inputs, outputs)
        return raw_tx
    except Exception as e:
        raise Exception(f"Failed to create raw transaction: {str(e)}")