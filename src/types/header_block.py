from typing import Optional, List
from dataclasses import dataclass

from src.types.end_of_slot_bundle import EndOfSubSlotBundle
from src.util.streamable import Streamable, streamable
from src.types.vdf import VDFProof
from src.types.reward_chain_sub_block import RewardChainSubBlock
from src.types.foliage import FoliageSubBlock, FoliageBlock


@dataclass(frozen=True)
@streamable
class HeaderBlock(Streamable):
    # Same as a FullBlock but without TransactionInfo and Generator (but with filter), used by light clients
    finished_sub_slots: List[EndOfSubSlotBundle]  # If first sb
    reward_chain_sub_block: RewardChainSubBlock  # Reward chain trunk data
    challenge_chain_sp_proof: VDFProof
    challenge_chain_ip_proof: VDFProof
    reward_chain_sp_proof: VDFProof
    reward_chain_ip_proof: VDFProof
    infused_challenge_chain_ip_proof: Optional[VDFProof]  # Iff deficit < 4
    foliage_sub_block: FoliageSubBlock  # Reward chain foliage data
    foliage_block: Optional[FoliageBlock]  # Reward chain foliage data (tx block)
    transactions_filter: bytes  # Filter for block transactions

    @property
    def prev_header_hash(self):
        return self.foliage_sub_block.prev_sub_block_hash

    @property
    def height(self):
        return self.reward_chain_sub_block.sub_block_height

    @property
    def weight(self):
        return self.reward_chain_sub_block.weight

    @property
    def header_hash(self):
        return self.foliage_sub_block.get_hash()

    @property
    def total_iters(self):
        return self.reward_chain_sub_block.total_iters
