from decouple import config
from evrmore_rpc import EvrmoreClient

RPC = EvrmoreClient(datadir=config('RPC_DATADIR', default='/tmp/evrmore'))
