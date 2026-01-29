from django.shortcuts import render
from .rpc import RPC

def explorer(request):
    """Display the 10 most recent blocks from the blockchain"""
    blocks = []
    error_message = None
    
    try:
        # Get the current block count (height)
        block_count = RPC.execute_command_sync('getblockcount')
        
        # Get the last 10 blocks
        for i in range(10):
            block_height = block_count - i
            if block_height < 0:
                break
            
            # Get block hash for this height
            block_hash = RPC.execute_command_sync('getblockhash', block_height)
            
            # Get block details
            block = RPC.execute_command_sync('getblock', block_hash)
            
            blocks.append({
                'height': block_height,
                'hash': block_hash,
                'time': block.get('time'),
                'tx_count': len(block.get('tx', [])),
                'size': block.get('size'),
            })
    except Exception as e:
        error_message = f"Error connecting to blockchain: {str(e)}"
    
    context = {
        'blocks': blocks,
        'error_message': error_message,
    }
    return render(request, 'explorer/index.html', context)
