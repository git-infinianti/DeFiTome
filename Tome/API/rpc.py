"""
RPC client wrapper for Evrmore blockchain operations.
This module provides access to all Evrmore RPC commands for the API.
"""

from Tome.rpc_client import RPC


class EvrmoreRPC:
    """Wrapper class for Evrmore RPC commands organized by category"""
    
    def __init__(self):
        self.client = RPC
    
    # ============================================================
    # ADDRESSINDEX COMMANDS
    # ============================================================
    
    def get_address_balance(self, address):
        """Get balance for a specific address"""
        return self.client.getaddressbalance(address)
    
    def get_address_deltas(self, addresses, start=None, end=None, chainInfo=False):
        """Get all changes for an address"""
        params = {"addresses": addresses}
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        if chainInfo:
            params["chainInfo"] = chainInfo
        return self.client.getaddressdeltas(params)
    
    def get_address_mempool(self, addresses):
        """Get mempool transactions for addresses"""
        return self.client.getaddressmempool({"addresses": addresses})
    
    def get_address_txids(self, addresses, start=None, end=None):
        """Get transaction IDs for addresses"""
        params = {"addresses": addresses}
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        return self.client.getaddresstxids(params)
    
    def get_address_utxos(self, addresses, chainInfo=False):
        """Get unspent transaction outputs for addresses"""
        params = {"addresses": addresses}
        if chainInfo:
            params["chainInfo"] = chainInfo
        return self.client.getaddressutxos(params)
    
    # ============================================================
    # ASSETS COMMANDS
    # ============================================================
    
    def get_asset_data(self, asset_name):
        """Get data about a specific asset"""
        return self.client.getassetdata(asset_name)
    
    def get_cache_info(self):
        """Get cache information"""
        return self.client.getcacheinfo()
    
    def get_snapshot(self, asset_name, block_height):
        """Get snapshot of asset holders at a specific block height"""
        return self.client.getsnapshot(asset_name, block_height)
    
    def issue_asset(
        self,
        asset_name,
        qty,
        to_address="",
        change_address="",
        units=0,
        reissuable=True,
        has_ipfs=False,
        ipfs_hash="",
        permanent_ipfs_hash="",
        toll_amount=0,
        toll_address="",
        toll_amount_mutability=False,
        toll_address_mutability=False,
        remintable=None,
    ):
        """Issue a new asset using the node's full issue parameter shape."""
        if remintable is None:
            remintable = bool(reissuable)

        return self.client.issue(
            asset_name,
            qty,
            to_address,
            change_address,
            units,
            reissuable,
            has_ipfs,
            ipfs_hash,
            permanent_ipfs_hash,
            toll_amount,
            toll_address,
            toll_amount_mutability,
            toll_address_mutability,
            remintable,
        )
    
    def issue_unique_asset(self, root_name, asset_tags, ipfs_hashes=None, 
                          to_address="", change_address=""):
        """Mint Evrmore NFTs as unique assets under an existing root asset."""
        return self.client.issueunique(root_name, asset_tags, ipfs_hashes or [], 
                                      to_address, change_address)
    
    def list_addresses_by_asset(self, asset_name, onlytotal=False, count=50, start=0):
        """List all addresses that own a specific asset"""
        return self.client.listaddressesbyasset(asset_name, onlytotal, count, start)
    
    def list_asset_balances_by_address(self, address, onlytotal=False, count=50, start=0):
        """List all assets owned by a specific address"""
        return self.client.listassetbalancesbyaddress(address, onlytotal, count, start)
    
    def list_assets(self, asset="*", verbose=False, count=50, start=0):
        """List all assets"""
        return self.client.listassets(asset, verbose, count, start)
    
    def list_my_assets(self, asset="*", verbose=False, count=50, start=0, confs=0):
        """List assets owned by this wallet"""
        return self.client.listmyassets(asset, verbose, count, start, confs)
    
    def purge_snapshot(self, asset_name, block_height):
        """Purge a snapshot"""
        return self.client.purgesnapshot(asset_name, block_height)
    
    def reissue_asset(self, asset_name, qty, to_address, change_address="", 
                     reissuable=True, new_units=-1, new_ipfs=""):
        """Reissue an existing asset"""
        return self.client.reissue(asset_name, qty, to_address, change_address, 
                                  reissuable, new_units, new_ipfs)
    
    def transfer_asset(self, asset_name, qty, to_address, message="", 
                      expire_time=0, change_address="", asset_change_address=""):
        """Transfer an asset"""
        return self.client.transfer(asset_name, qty, to_address, message, 
                                   expire_time, change_address, asset_change_address)
    
    def transfer_from_address(self, asset_name, from_address, qty, to_address, 
                             message="", expire_time=0, evr_change_address="", 
                             asset_change_address=""):
        """Transfer asset from a specific address"""
        return self.client.transferfromaddress(asset_name, from_address, qty, to_address, 
                                              message, expire_time, evr_change_address, 
                                              asset_change_address)
    
    def transfer_from_addresses(self, asset_name, from_addresses, qty, to_address, 
                               message="", expire_time=0, evr_change_address="", 
                               asset_change_address=""):
        """Transfer asset from multiple addresses"""
        return self.client.transferfromaddresses(asset_name, from_addresses, qty, to_address, 
                                                message, expire_time, evr_change_address, 
                                                asset_change_address)
    
    # ============================================================
    # VAULT & NFT ASSET COMMANDS
    # ============================================================
    
    def issue_vault_asset(self, asset_name, qty, to_address="", change_address="", 
                         units=0, reissuable=False, has_ipfs=False, ipfs_hash="",
                         toll_percentage=0, toll_address=""):
        """
        Issue a vault asset with toll/fee features for DeFi operations.
        
        Args:
            asset_name: Name of the vault asset
            qty: Quantity to issue
            to_address: Destination address (default: new address in wallet)
            change_address: Change address (default: new address in wallet)
            units: Decimal places (0-8)
            reissuable: Whether asset can be reissued
            has_ipfs: Whether asset has IPFS metadata
            ipfs_hash: IPFS hash for metadata
            toll_percentage: Transfer fee percentage (0-100)
            toll_address: Address to receive toll payments
        
        Returns:
            Transaction hash
            
        Note:
            Toll parameters are prepared for Evrmore Core V2 release.
            Current implementation uses standard asset issuance.
            
            TODO: When Evrmore Core V2 is released with toll support, update to:
            - Add toll_percentage to RPC parameters
            - Add toll_address to RPC parameters
            - Enable NFT royalty features
        """
        # Build the issue command with basic parameters
        # Toll support will be added in future Evrmore Core V2 release
        
        return self.issue_asset(
            asset_name,
            qty,
            to_address,
            change_address,
            units,
            reissuable,
            has_ipfs,
            ipfs_hash,
            toll_amount=toll_percentage,
            toll_address=toll_address,
            remintable=bool(reissuable),
        )
    
    def get_vault_toll_info(self, asset_name):
        """
        Get toll information for a vault asset.
        
        Args:
            asset_name: Name of the vault asset
            
        Returns:
            Dict with toll_percentage, toll_address, total_toll_collected
        """
        asset_data = self.get_asset_data(asset_name)
        
        # Extract toll info from asset data when available
        toll_info = {
            'has_toll': asset_data.get('has_toll', False),
            'toll_percentage': asset_data.get('toll_percentage', 0),
            'toll_address': asset_data.get('toll_address', ''),
            'total_toll_collected': asset_data.get('total_toll_collected', 0)
        }
        
        return toll_info
    
    def issue_nft_asset(self, asset_name, to_address="", change_address="", 
                       ipfs_hash="", description=""):
        """
        Mint an NFT as an Evrmore unique asset named ROOT#TAG.
        
        Args:
            asset_name: NFT asset name in ROOT#TAG form
            to_address: Destination address
            change_address: Change address
            ipfs_hash: IPFS hash for NFT metadata/image
            description: NFT description
            
        Returns:
            Transaction hash
        """
        root_name, separator, asset_tag = str(asset_name).partition('#')
        if not separator or not root_name or not asset_tag or '#' in asset_tag:
            raise ValueError('NFT asset_name must use the Evrmore ROOT#TAG format.')

        return self.issue_unique_asset(
            root_name=root_name,
            asset_tags=[asset_tag],
            ipfs_hashes=[ipfs_hash] if ipfs_hash else None,
            to_address=to_address,
            change_address=change_address,
        )
    
    def verify_asset_ownership(self, asset_name, address):
        """
        Verify ownership of an asset at a specific address.
        
        Args:
            asset_name: Name of the asset
            address: Address to check
            
        Returns:
            Dict with ownership info
        """
        try:
            balances = self.list_asset_balances_by_address(address)
            
            for asset_info in balances:
                if asset_info.get('assetName') == asset_name:
                    return {
                        'owns_asset': True,
                        'balance': asset_info.get('balance', 0),
                        'asset_name': asset_name,
                        'address': address
                    }
            
            return {
                'owns_asset': False,
                'balance': 0,
                'asset_name': asset_name,
                'address': address
            }
        except Exception as e:
            return {
                'owns_asset': False,
                'balance': 0,
                'error': str(e)
            }
    
    # ============================================================
    # BLOCKCHAIN COMMANDS
    # ============================================================
    
    def clear_mempool(self):
        """Clear the memory pool"""
        return self.client.clearmempool()
    
    def decode_block(self, blockhex):
        """Decode a block from hex"""
        return self.client.decodeblock(blockhex)
    
    def get_best_block_hash(self):
        """Get the hash of the best (tip) block"""
        return self.client.getbestblockhash()
    
    def get_block(self, blockhash, verbosity=1):
        """Get block information"""
        return self.client.getblock(blockhash, verbosity)
    
    def get_blockchain_info(self):
        """Get blockchain information"""
        return self.client.getblockchaininfo()
    
    def get_block_count(self):
        """Get the current block count"""
        return self.client.getblockcount()
    
    def get_block_hash(self, height):
        """Get block hash for a specific height"""
        return self.client.getblockhash(height)
    
    def get_block_hashes(self, timestamp):
        """Get block hashes for a timestamp"""
        return self.client.getblockhashes(timestamp)
    
    def get_block_header(self, block_hash, verbose=True):
        """Get block header"""
        return self.client.getblockheader(block_hash, verbose)
    
    def get_chain_tips(self):
        """Get information about all known tips in the block tree"""
        return self.client.getchaintips()
    
    def get_chain_tx_stats(self, nblocks=None, blockhash=None):
        """Get statistics about the total number and rate of transactions"""
        return self.client.getchaintxstats(nblocks, blockhash)
    
    def get_difficulty(self):
        """Get the current difficulty"""
        return self.client.getdifficulty()
    
    def get_mempool_ancestors(self, txid, verbose=False):
        """Get mempool ancestors"""
        return self.client.getmempoolancestors(txid, verbose)
    
    def get_mempool_descendants(self, txid, verbose=False):
        """Get mempool descendants"""
        return self.client.getmempooldescendants(txid, verbose)
    
    def get_mempool_entry(self, txid):
        """Get mempool entry"""
        return self.client.getmempoolentry(txid)
    
    def get_mempool_info(self):
        """Get mempool information"""
        return self.client.getmempoolinfo()
    
    def get_raw_mempool(self, verbose=False):
        """Get raw mempool"""
        return self.client.getrawmempool(verbose)
    
    def get_spent_info(self, txid, index):
        """Get spent info"""
        return self.client.getspentinfo({"txid": txid, "index": index})
    
    def get_tx_out(self, txid, n, include_mempool=True):
        """Get transaction output"""
        return self.client.gettxout(txid, n, include_mempool)
    
    def get_tx_out_proof(self, txids, blockhash=None):
        """Get proof that transactions are in a block"""
        return self.client.gettxoutproof(txids, blockhash)
    
    def get_tx_out_set_info(self):
        """Get statistics about the unspent transaction output set"""
        return self.client.gettxoutsetinfo()
    
    def precious_block(self, blockhash):
        """Treat a block as if it were received before others with the same work"""
        return self.client.preciousblock(blockhash)
    
    def prune_blockchain(self, height):
        """Prune the blockchain"""
        return self.client.pruneblockchain(height)
    
    def save_mempool(self):
        """Save the mempool to disk"""
        return self.client.savemempool()
    
    def verify_chain(self, checklevel=3, nblocks=6):
        """Verify the blockchain database"""
        return self.client.verifychain(checklevel, nblocks)
    
    def verify_tx_out_proof(self, proof):
        """Verify that a proof points to a transaction in a block"""
        return self.client.verifytxoutproof(proof)
    
    # ============================================================
    # CONTROL COMMANDS
    # ============================================================
    
    def get_info(self):
        """Get general information about the Evrmore node"""
        return self.client.getinfo()
    
    def get_memory_info(self, mode="stats"):
        """Get memory usage information"""
        return self.client.getmemoryinfo(mode)
    
    def get_rpc_info(self):
        """Get RPC server information"""
        return self.client.getrpcinfo()
    
    def help(self, command=None):
        """Get help for RPC commands"""
        return self.client.help(command)
    
    def stop_node(self):
        """Stop the Evrmore node"""
        return self.client.stop()
    
    def uptime(self):
        """Get node uptime"""
        return self.client.uptime()
    
    # ============================================================
    # GENERATING COMMANDS
    # ============================================================
    
    def generate(self, nblocks, maxtries=1000000):
        """Generate blocks immediately (RegTest only)"""
        return self.client.generate(nblocks, maxtries)
    
    def generate_to_address(self, nblocks, address, maxtries=1000000):
        """Generate blocks to a specific address"""
        return self.client.generatetoaddress(nblocks, address, maxtries)
    
    def get_generate(self):
        """Check if the server is set to generate coins"""
        return self.client.getgenerate()
    
    def set_generate(self, generate, genproclimit=-1):
        """Set generation on or off"""
        return self.client.setgenerate(generate, genproclimit)
    
    # ============================================================
    # MESSAGES COMMANDS
    # ============================================================
    
    def clear_messages(self):
        """Clear all messages"""
        return self.client.clearmessages()
    
    def send_message(self, channel_name, ipfs_hash, expire_time=0):
        """Send a message to a channel"""
        return self.client.sendmessage(channel_name, ipfs_hash, expire_time)
    
    def subscribe_to_channel(self, channel_name):
        """Subscribe to a message channel"""
        return self.client.subscribetochannel(channel_name)
    
    def unsubscribe_from_channel(self, channel_name):
        """Unsubscribe from a message channel"""
        return self.client.unsubscribefromchannel(channel_name)
    
    def view_all_message_channels(self):
        """View all message channels"""
        return self.client.viewallmessagechannels()
    
    def view_all_messages(self):
        """View all messages"""
        return self.client.viewallmessages()
    
    # ============================================================
    # MINING COMMANDS
    # ============================================================
    
    def get_block_template(self, template_request=None):
        """Get block template for mining"""
        return self.client.getblocktemplate(template_request)
    
    def get_evr_progpow_hash(self, header_hash, mix_hash, nonce, height, target):
        """Get ProgPoW hash"""
        return self.client.getevrprogpowhash(header_hash, mix_hash, nonce, height, target)
    
    def get_mining_info(self):
        """Get mining information"""
        return self.client.getmininginfo()
    
    def get_network_hash_ps(self, nblocks=120, height=-1):
        """Get estimated network hashes per second"""
        return self.client.getnetworkhashps(nblocks, height)
    
    def pprpcsb(self, header_hash, mix_hash, nonce):
        """ProgPoW RPC submit block"""
        return self.client.pprpcsb(header_hash, mix_hash, nonce)
    
    def prioritise_transaction(self, txid, priority_delta, fee_delta):
        """Prioritise a transaction"""
        return self.client.prioritisetransaction(txid, priority_delta, fee_delta)


# Global instance for easy import
evrmore_rpc = EvrmoreRPC()
