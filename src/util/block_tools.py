import os
import shutil
import sys
import tempfile
from argparse import Namespace
from pathlib import Path
from random import seed
from typing import Dict, List, Tuple, Optional

from Crypto.Random import random
from blspy import G1Element, G2Element, AugSchemeMPL
from chiabip158 import PyBIP158
from chiavdf import prove

from src.cmds.init import create_default_chia_config, initialize_ssl
from src.cmds.plots import create_plots
from src.consensus import pot_iterations, block_rewards
from src.consensus.coinbase import (
    create_puzzlehash_for_pk,
    create_pool_coin,
    create_farmer_coin,
)
from src.consensus.constants import ConsensusConstants
from src.consensus.pot_iterations import (
    calculate_infusion_point_iters,
    calculate_iterations_quality,
    calculate_infusion_challenge_point_iters,
    calculate_min_iters_from_iterations,
    is_overflow_sub_block,
)
from src.full_node.difficulty_adjustment import (
    get_next_slot_iters,
    get_next_difficulty,
    get_next_ips,
)
from src.full_node.mempool_check_conditions import get_name_puzzle_conditions
from src.full_node.sub_block_record import SubBlockRecord
from src.plotting.plot_tools import load_plots
from src.types import proof_of_space
from src.types.challenge_slot import ChallengeSlot
from src.types.classgroup import ClassgroupElement
from src.types.coin import hash_coin_list, Coin
from src.types.foliage import (
    FoliageBlock,
    FoliageSubBlock,
    TransactionsInfo,
    FoliageSubBlockData,
)
from src.types.full_block import FullBlock, additions_for_npc
from src.types.pool_target import PoolTarget
from src.types.program import Program
from src.types.proof_of_space import ProofOfSpace
from src.types.reward_chain_end_of_slot import RewardChainEndOfSlot, EndOfSlotProofs
from src.types.reward_chain_sub_block import RewardChainSubBlock
from src.types.sized_bytes import bytes32
from src.types.vdf import VDFInfo, VDFProof
from src.util.hash import std_hash
from src.util.ints import uint32, uint64, int512, uint128
from src.util.keychain import Keychain, bytes_to_mnemonic
from src.util.logging import initialize_logging
from src.util.merkle_set import MerkleSet
from src.util.path import mkdir
from src.util.wallet_tools import WalletTool
from src.wallet.derive_keys import (
    master_sk_to_farmer_sk,
    master_sk_to_pool_sk,
    master_sk_to_wallet_sk,
)


def get_plot_dir():
    cache_path = (
            Path(os.path.expanduser(os.getenv("CHIA_ROOT", "~/.chia/"))) / "test-plots"
    )
    mkdir(cache_path)
    return cache_path


def get_challenge_chain_icp_vdf(
        block: FullBlock, constants: ConsensusConstants, ips: uint64, required_iters: uint64
) -> VDFInfo:
    ip_iters: uint64 = calculate_infusion_point_iters(constants, ips, required_iters)
    cc_vdf_challenge: bytes32 = block.finished_slots[-1][0].get_hash()

    return VDFInfo(
        cc_vdf_challenge,
        ip_iters,
        block.reward_chain_sub_block.challenge_chain_ip_vdf.output,
    )


def get_challenge_chain_ip_vdf(
        block: FullBlock, constants: ConsensusConstants, ips: uint64, required_iters: uint64
) -> VDFInfo:
    ip_iters: uint64 = calculate_infusion_challenge_point_iters(
        constants, ips, required_iters
    )
    cc_vdf_challenge: bytes32 = block.finished_slots[-1][0].get_hash()

    return VDFInfo(
        cc_vdf_challenge,
        ip_iters,
        block.reward_chain_sub_block.challenge_chain_icp_vdf.output,
    )


def get_reward_chain_icp_vdf(
        block: FullBlock, constants: ConsensusConstants, ips: uint64, required_iters: uint64
) -> VDFInfo:
    ip_iters: uint64 = calculate_infusion_point_iters(constants, ips, required_iters)
    cc_vdf_challenge: bytes32 = block.finished_slots[-1][0].get_hash()

    return VDFInfo(
        cc_vdf_challenge,
        ip_iters,
        block.reward_chain_sub_block.reward_chain_ip_vdf.output,
    )


def get_reward_chain_ip_vdf(
        block: FullBlock, constants: ConsensusConstants, ips: uint64, required_iters: uint64
) -> VDFInfo:
    ip_iters: uint64 = calculate_infusion_challenge_point_iters(
        constants, ips, required_iters
    )
    cc_vdf_challenge: bytes32 = block.finished_slots[-1][0].get_hash()

    return VDFInfo(
        cc_vdf_challenge,
        ip_iters,
        block.reward_chain_sub_block.reward_chain_icp_vdf.output,
    )


def is_transaction_block(
        overflow: bool, total_iters, ip_iters, icp_iters, slot_iters, curr_total_iters
) -> bool:
    # The first sub-block to have an icp > the last block's infusion iters, is a block
    if overflow:
        our_icp_total_iters: uint128 = uint128(
            total_iters - ip_iters + icp_iters - slot_iters
        )
    else:
        our_icp_total_iters: uint128 = uint128(total_iters - ip_iters + icp_iters)
    return our_icp_total_iters > curr_total_iters


class BlockTools:
    """
    Tools to generate blocks for testing.
    """

    def __init__(
            self,
            root_path: Optional[Path] = None,
            real_plots: bool = False,
    ):
        self._tempdir = None
        if root_path is None:
            self._tempdir = tempfile.TemporaryDirectory()
            root_path = Path(self._tempdir.name)
        self.root_path = root_path
        self.real_plots = real_plots

        if not real_plots:
            create_default_chia_config(root_path)
            initialize_ssl(root_path)
            # No real plots supplied, so we will use the small test plots
            self.use_any_pos = True
            self.keychain = Keychain("testing-1.8.0", True)
            self.keychain.delete_all_keys()
            self.farmer_master_sk = self.keychain.add_private_key(
                bytes_to_mnemonic(std_hash(b"block_tools farmer key")), ""
            )
            self.pool_master_sk = self.keychain.add_private_key(
                bytes_to_mnemonic(std_hash(b"block_tools pool key")), ""
            )
            self.farmer_pk = master_sk_to_farmer_sk(self.farmer_master_sk).get_g1()
            self.pool_pk = master_sk_to_pool_sk(self.pool_master_sk).get_g1()
            self.reward_chain_head = None
            self.challenge_chain_head = FullBlock
            self.tx_height = None
            self.prev_foliage_block = None

            plot_dir = get_plot_dir()
            mkdir(plot_dir)
            temp_dir = plot_dir / "tmp"
            mkdir(temp_dir)
            args = Namespace()
            # Can't go much lower than 18, since plots start having no solutions
            args.size = 18
            # Uses many plots for testing, in order to guarantee proofs of space at every height
            args.num = 40
            args.buffer = 100
            args.farmer_public_key = bytes(self.farmer_pk).hex()
            args.pool_public_key = bytes(self.pool_pk).hex()
            args.tmp_dir = temp_dir
            args.tmp2_dir = plot_dir
            args.final_dir = plot_dir
            args.plotid = None
            args.memo = None
            args.buckets = 0
            args.stripe_size = 2000
            args.num_threads = 0
            test_private_keys = [
                AugSchemeMPL.key_gen(std_hash(bytes([i]))) for i in range(args.num)
            ]
            try:
                # No datetime in the filename, to get deterministic filenames and not replot
                create_plots(
                    args,
                    root_path,
                    use_datetime=False,
                    test_private_keys=test_private_keys,
                )
            except KeyboardInterrupt:
                shutil.rmtree(plot_dir, ignore_errors=True)
                sys.exit(1)
        else:
            initialize_ssl(root_path)
            self.keychain = Keychain()
            self.use_any_pos = False
            sk_and_ent = self.keychain.get_first_private_key()
            assert sk_and_ent is not None
            self.farmer_master_sk = sk_and_ent[0]
            self.pool_master_sk = sk_and_ent[0]

        self.farmer_ph = create_puzzlehash_for_pk(
            master_sk_to_wallet_sk(self.farmer_master_sk, uint32(0)).get_g1()
        )
        self.pool_ph = create_puzzlehash_for_pk(
            master_sk_to_wallet_sk(self.pool_master_sk, uint32(0)).get_g1()
        )

        self.all_sks = self.keychain.get_all_private_keys()
        self.pool_pubkeys: List[G1Element] = [
            master_sk_to_pool_sk(sk).get_g1() for sk, _ in self.all_sks
        ]
        self.curr_slot = 1
        self.curr_epoch = 1
        self.curr_sub_epoch = 1
        self.sub_blocks = Dict[bytes32, SubBlockRecord]
        self.height_to_hash = Dict[uint32, bytes32]
        self.finished_slots = List[
            Tuple[ChallengeSlot, RewardChainEndOfSlot, EndOfSlotProofs]
        ]
        self.ips = 0
        self.deficit = 0
        self.number_iters = 0
        self.proof_of_space = None
        self.quality = 0
        self.plot_pk = None
        self.slot_iters = 0

        farmer_pubkeys: List[G1Element] = [
            master_sk_to_farmer_sk(sk).get_g1() for sk, _ in self.all_sks
        ]
        if len(self.pool_pubkeys) == 0 or len(farmer_pubkeys) == 0:
            raise RuntimeError("Keys not generated. Run `chia generate keys`")
        _, self.plots, _, _ = load_plots(
            {}, {}, farmer_pubkeys, self.pool_pubkeys, root_path
        )

    def get_plot_signature(self, m: bytes32, plot_pk: G1Element) -> Optional[G2Element]:
        """
        Returns the plot signature of the header data.
        """
        farmer_sk = master_sk_to_farmer_sk(self.all_sks[0][0])
        for _, plot_info in self.plots.items():
            agg_pk = ProofOfSpace.generate_plot_public_key(
                plot_info.local_sk.get_g1(), plot_info.farmer_public_key
            )
            if agg_pk == plot_pk:
                harv_share = AugSchemeMPL.sign(plot_info.local_sk, m, agg_pk)
                farm_share = AugSchemeMPL.sign(farmer_sk, m, agg_pk)
                return AugSchemeMPL.aggregate([harv_share, farm_share])

        return None

    def get_pool_key_signature(
            self, pool_target: PoolTarget, pool_pk: G1Element
    ) -> Optional[G2Element]:
        for sk, _ in self.all_sks:
            sk_child = master_sk_to_pool_sk(sk)
            if sk_child.get_g1() == pool_pk:
                return AugSchemeMPL.sign(sk_child, bytes(pool_target))
        return None

    def get_farmer_wallet_tool(self) -> WalletTool:
        return WalletTool(self.farmer_master_sk)

    def get_pool_wallet_tool(self) -> WalletTool:
        return WalletTool(self.pool_master_sk)

    def get_consecutive_blocks(
            self,
            test_constants: ConsensusConstants,
            num_blocks: int,
            block_list: List[FullBlock] = [],
            reward_puzzlehash: bytes32 = None,
            fees: uint64 = uint64(0),
            transaction_data_at_height: Dict[int, Tuple[Program, G2Element]] = None,
    ) -> List[FullBlock]:

        if transaction_data_at_height is None:
            transaction_data_at_height = {}
        if len(block_list) == 0:
            # create genesis
            genesis = self.create_genesis_block(test_constants)
            difficulty = test_constants.DIFFICULTY_STARTING
            curr_min_iters = test_constants.MIN_ITERS_STARTING
            self.challenge_chain_head = genesis
            self.ips = test_constants.IPS_STARTING
            block_list.append(genesis)
            self.deficit = 5
            block_list.append(genesis)
            self.prev_foliage_block = genesis.foliage_block
        # todo do we need this delay in new protocol ?
        elif len(block_list) < (
                test_constants.DIFFICULTY_EPOCH + test_constants.DIFFICULTY_DELAY
        ):
            # First epoch (+delay), so just get first difficulty
            prev_difficulty = block_list[0].weight
            difficulty = block_list[0].weight
            assert test_constants.DIFFICULTY_STARTING == prev_difficulty
            curr_min_iters = test_constants.MIN_ITERS_STARTING
        else:
            difficulty = block_list[-1].weight - block_list[-2].weight
            assert block_list[-1].proof_of_time is not None
            curr_min_iters = calculate_min_iters_from_iterations(
                block_list[-1].proof_of_space,
                difficulty,
                block_list[-1].proof_of_time.number_of_iterations,
                test_constants.NUMBER_ZERO_BITS_CHALLENGE_SIG,
            )

        starting_height = block_list[-1].height + 1
        timestamp = block_list[-1].header.data.timestamp
        end_of_slot: RewardChainEndOfSlot = None
        sub_blocks = None
        for next_height in range(starting_height, starting_height + num_blocks):
            if next_height in transaction_data_at_height:
                transactions, aggsig = transaction_data_at_height[next_height]

            # update values
            prev_block = block_list[-1]
            sub_blocks[prev_block.get_hash()] = prev_block.get_sub_block_record()
            self.height_to_hash[prev_block.height] = (prev_block.get_hash(),)
            self.slot_iters = get_next_slot_iters(
                test_constants,
                self.height_to_hash,
                sub_blocks,
                block_list[-1].reward_chain_sub_block.get_hash(),
            )

            # check is new slot
            if self.number_iters > self.slot_iters:
                new_slot = True
                self.finished_slots.append(
                    Tuple[ChallengeSlot(), RewardChainEndOfSlot(), EndOfSlotProofs()]
                )
                (
                    self.number_iters,
                    self.proof_of_space,
                    self.quality,
                    self.plot_pk,
                ) = get_prams_from_plots(
                    test_constants, std_hash(end_of_slot), difficulty, curr_min_iters
                )
                # todo handle multiple slots between blocks
                self.finished_slots = List[
                    Tuple[ChallengeSlot, RewardChainEndOfSlot, EndOfSlotProofs]
                ]

            # is end of sub_epoch
            if len(self.sub_blocks) == 384 * (self.curr_sub_epoch + 1):
                # is end of epoch
                if len(self.sub_blocks) == 32256 * (self.curr_epoch + 1):
                    # new difficulty
                    difficulty = get_next_difficulty(
                        test_constants,
                        self.sub_blocks,
                        self.height_to_hash,
                        block_list[-1].header_hash,
                        new_slot,
                    )
                    # new iterations per slot
                    self.ips = get_next_ips(
                        test_constants,
                        self.height_to_hash,
                        sub_blocks,
                        block_list[-1].header_hash,
                    )

            # keep track of deficit
            if self.deficit > 0:
                self.deficit = self.deficit - 1

            block = self.create_next_block(
                test_constants,
                difficulty,
                fees,
                block_list[-1].reward_chain_sub_block,
                block_list[-1].weight,
                transactions,
                aggsig,
                timestamp,
                reward_puzzlehash,
                proof_of_space,
                end_of_slot,
            )

            # check if challenge chain block
            if block.challenge_chain_ip_proof:
                challenge_hash = (
                    block.reward_chain_sub_block.proof_of_space.challenge_hash
                )
                self.deficit = 5
                sub_blocks = Dict  # new challenge chain block, zero sub block list
                self.challenge_chain_head = block

            block_list.append(block)

        return block_list

    def create_genesis_block(
            self, test_constants: ConsensusConstants, proof_of_space
    ) -> FullBlock:
        """
        Creates the genesis block with the specified details.
        """

        required_iters: uint64 = calculate_iterations_quality(
            4, proof_of_space.size, test_constants.DIFFICULTY_STARTING
        )

        icp_iters: uint64 = calculate_infusion_challenge_point_iters(
            test_constants, uint64(test_constants.IPS_STARTING), required_iters
        )

        ip_iters: uint64 = calculate_infusion_point_iters(
            test_constants, uint64(test_constants.IPS_STARTING), required_iters
        )

        challenge_chain_icp_vdf = (
            VDFInfo(
                test_constants.FIRST_CC_CHALLENGE,
                ip_iters,
                test_constants.FIRST_CC_CHALLENGE,
            ),
        )
        challenge_chain_ip_vdf = (
            VDFInfo(
                test_constants.FIRST_CC_CHALLENGE,
                icp_iters,
                test_constants.FIRST_CC_CHALLENGE,
            ),
        )
        challenge_chain_icp_proof = (
            get_vdf_proof(
                test_constants.FIRST_CC_CHALLENGE,
                str(1),
                str(2),
                self.number_iters,
                test_constants.DISCRIMINANT_SIZE_BITS,
            ),
        )
        challenge_chain_icp_signature = (
            self.get_plot_signature(self.challenge_chain_head, self.plot_pk),
        )
        challenge_chain_ip_proof = (
            get_vdf_proof(
                test_constants.FIRST_CC_CHALLENGE,
                str(1),
                str(2),
                self.number_iters,
                test_constants.DISCRIMINANT_SIZE_BITS,
            ),
        )
        reward_chain_icp_proof = (
            get_vdf_proof(
                test_constants.FIRST_RC_CHALLENGE,
                str(1),
                str(2),
                test_constants.DISCRIMINANT_SIZE_BITS,
                self.number_iters,
            ),
        )
        reward_chain_ip_proof = (
            get_vdf_proof(
                test_constants.FIRST_RC_CHALLENGE,
                str(1),
                str(2),
                test_constants.DISCRIMINANT_SIZE_BITS,
                self.number_iters,
            ),
        )

        # todo fix no head in genesis
        head = None
        reward_chain_icp_vdf = get_reward_chain_icp_vdf(head, test_constants, icp_iters)
        reward_chain_icp_sig = get_reward_chain_ip_vdf(head, test_constants, ip_iters)
        reward_chain_ip_vdf = self.get_plot_signature(head, self.plot_pk)

        reward_chain_sub_block = RewardChainSubBlock(
            test_constants.DIFFICULTY_STARTING,
            self.number_iters,
            proof_of_space,
            challenge_chain_ip_vdf,
            challenge_chain_icp_vdf,
            challenge_chain_icp_signature,
            reward_chain_icp_vdf,
            reward_chain_icp_sig,
            reward_chain_ip_vdf,
        )

        # todo genesis foliage

        (
            self.number_iters,
            self.proof_of_space,
            self.quality,
            self.plot_pk,
        ) = get_prams_from_plots(
            test_constants,
            test_constants.FIRST_RC_CHALLENGE,
            test_constants.DIFFICULTY_STARTING,
            test_constants.MIN_ITERS_STARTING,
        )

        full_block: FullBlock = FullBlock(
            challenge_chain_icp_vdf=challenge_chain_icp_vdf,
            challenge_chain_ip_vdf=challenge_chain_ip_vdf,
            challenge_chain_icp_proof=challenge_chain_icp_proof,
            challenge_chain_icp_signature=challenge_chain_icp_signature,
            challenge_chain_ip_proof=challenge_chain_ip_proof,
            reward_chain_icp_proof=reward_chain_icp_proof,
            reward_chain_ip_proof=reward_chain_ip_proof,
            reward_chain_sub_block=reward_chain_sub_block,
            # foliage_sub_block=foliage_sub_block,
            # foliage_block=foliage_block,
        )

        return full_block

    def create_next_block(
            self,
            test_constants: ConsensusConstants,
            difficulty: int,
            fees: uint64,
            head: FullBlock,
            previous_weight: uint128,
            transactions: Optional[Program],
            aggsig: Optional[G2Element],
            timestamp: uint64,
            reward_puzzlehash: bytes32,
            end_of_slot: RewardChainEndOfSlot,
    ) -> FullBlock:
        """
        Creates the next block with the specified details.
        """

        required_iters: uint64 = calculate_iterations_quality(
            self.quality, proof_of_space.size, difficulty
        )
        icp_iters: uint64 = calculate_infusion_challenge_point_iters(
            test_constants, self.ips, required_iters
        )
        ip_iters: uint64 = calculate_infusion_point_iters(
            test_constants, self.ips, required_iters
        )
        challenge_chain_icp_vdf = get_challenge_chain_icp_vdf(
            head, test_constants, icp_iters
        )
        challenge_chain_ip_vdf = get_challenge_chain_ip_vdf(
            head, test_constants, ip_iters
        )
        challenge_chain_icp_signature = self.get_plot_signature(
            self.challenge_chain_head, self.plot_pk
        )

        reward_chain_icp_vdf = get_reward_chain_icp_vdf(head, test_constants, icp_iters)
        reward_chain_icp_sig = get_reward_chain_ip_vdf(head, test_constants, ip_iters)
        reward_chain_ip_vdf = self.get_plot_signature(head, self.plot_pk)

        if self.deficit > 0:
            # dont create challenge block:
            is_block = False
            challenge_chain_icp_vdf = (None,)
            challenge_chain_ip_proof = None
            challenge_chain_icp_signature = None
            challenge_chain_ip_vdf = None
        else:
            challenge_chain_ip_proof = (
                get_vdf_proof(
                    std_hash(end_of_slot),
                    str(challenge_chain_ip_vdf.output.a),
                    str(challenge_chain_ip_vdf.output.b),
                    test_constants.DISCRIMINANT_SIZE_BITS,
                    self.number_iters,
                ),
            )

        reward_chain_sub_block = RewardChainSubBlock(
            previous_weight + difficulty,
            self.number_iters,
            proof_of_space,
            challenge_chain_ip_vdf,
            challenge_chain_icp_vdf,
            challenge_chain_icp_signature,
            reward_chain_icp_vdf,
            reward_chain_icp_sig,
            reward_chain_ip_vdf,
        )

        overflow = is_overflow_sub_block(test_constants, self.ips, required_iters)
        (
            foliage_sub_block,
            foliage_block,
            transactions_info,
            transactions_generator,
        ) = self.create_foliage(
            self.tx_height,
            fees,
            aggsig,
            transactions,
            block_rewards,
            self.plot_pk,
            self.prev_foliage_block,
            head.get_hash(),
            is_block,
            timestamp,
            self.challenge_chain_head.get_hash(),
            head.reward_chain_sub_block.get_unfinished().get_hash(),
            is_transaction_block(
                overflow,
                self.number_iters,
                ip_iters,
                icp_iters,
                self.slot_iters,
                head.total_iters,
            ),
            reward_puzzlehash,
        )

        self.prev_foliage_block = foliage_block

        full_block: FullBlock = FullBlock(
            finished_slots=self.finished_slots,
            challenge_chain_ip_proof=challenge_chain_ip_proof,
            reward_chain_sub_block=reward_chain_sub_block,
            reward_chain_icp_proof=get_vdf_proof(
                std_hash(end_of_slot),
                str(1),
                str(2),
                test_constants.DISCRIMINANT_SIZE_BITS,
                self.number_iters,
            ),
            reward_chain_ip_proof=get_vdf_proof(
                std_hash(end_of_slot),
                str(1),
                str(2),
                test_constants.DISCRIMINANT_SIZE_BITS,
                self.number_iters,
            ),
            foliage_sub_block=foliage_sub_block,
            foliage_block=foliage_block,
            transactions_info=transactions_info,
            transactions_generator=transactions_generator,
        )

        return full_block

    def create_foliage(
            self,
            height: uint32,
            fees: uint64,
            aggsig: G2Element,
            transactions: Program,
            reward_claims_incorporated: List[Coin],
            plot_pk: G2Element,
            prev_foliage_block: FoliageBlock,
            reward_block_hash: bytes32,
            is_block: bool,
            timestamp: uint64,
            prev_block_hash,
            unfinished_reward_block_hash: bytes32,
            is_transaction: bool,
            reward_puzzlehash: bytes32 = None,
    ) -> (FoliageSubBlock, FoliageBlock, TransactionsInfo, Program):

        # Use the extension data to create different blocks based on header hash
        extension_data: bytes32 = bytes32([random.randint(0, 255) for _ in range(32)])
        cost = uint64(0)

        fee_reward = uint64(block_rewards.calculate_base_farmer_reward(height) + fees)

        # Create filter
        byte_array_tx: List[bytes32] = []
        tx_additions: List[Coin] = []
        tx_removals: List[bytes32] = []
        if transactions:
            error, npc_list, _ = get_name_puzzle_conditions(transactions)
            additions: List[Coin] = additions_for_npc(npc_list)
            for coin in additions:
                tx_additions.append(coin)
                byte_array_tx.append(bytearray(coin.puzzle_hash))
            for npc in npc_list:
                tx_removals.append(npc.coin_name)
                byte_array_tx.append(bytearray(npc.coin_name))
        farmer_ph = self.farmer_ph
        pool_ph = self.pool_ph
        if reward_puzzlehash is not None:
            farmer_ph = reward_puzzlehash
            pool_ph = reward_puzzlehash

        byte_array_tx.append(bytearray(farmer_ph))
        byte_array_tx.append(bytearray(pool_ph))
        bip158: PyBIP158 = PyBIP158(byte_array_tx)
        encoded = bytes(bip158.GetEncoded())

        removal_merkle_set = MerkleSet()
        addition_merkle_set = MerkleSet()

        # Create removal Merkle set
        for coin_name in tx_removals:
            removal_merkle_set.add_already_hashed(coin_name)

        # Create addition Merkle set
        puzzlehash_coin_map: Dict[bytes32, List[Coin]] = {}
        pool_coin = create_pool_coin(height, reward_puzzlehash, fee_reward)
        farmer_coin = create_farmer_coin(height, reward_puzzlehash, fee_reward)

        for coin in tx_additions + [pool_coin, farmer_coin]:
            if coin.puzzle_hash in puzzlehash_coin_map:
                puzzlehash_coin_map[coin.puzzle_hash].append(coin)
            else:
                puzzlehash_coin_map[coin.puzzle_hash] = [coin]

        # Addition Merkle set contains puzzlehash and hash of all coins with that puzzlehash
        for puzzle, coins in puzzlehash_coin_map.items():
            addition_merkle_set.add_already_hashed(puzzle)
            addition_merkle_set.add_already_hashed(hash_coin_list(coins))

        additions_root = addition_merkle_set.get_root()
        removals_root = removal_merkle_set.get_root()

        generator_hash = (
            transactions.get_tree_hash()
            if transactions is not None
            else bytes32([0] * 32)
        )
        filter_hash = std_hash(encoded)

        pool_target = PoolTarget(pool_ph, uint32(height))
        pool_target_signature = self.get_pool_key_signature(
            pool_target, proof_of_space.pool_public_key
        )
        assert pool_target_signature is not None
        final_aggsig: G2Element = pool_target_signature
        if aggsig is not None:
            final_aggsig = AugSchemeMPL.aggregate([final_aggsig, aggsig])

        foliage_sub_block_data = FoliageSubBlockData(
            unfinished_reward_block_hash,
            pool_target,
            pool_target_signature,
            farmer_ph,
            extension_data,
            prev_foliage_block.get_hash(),
        )

        plot_key_signature: G2Element = self.get_plot_signature(
            foliage_sub_block_data, plot_pk
        )

        foliage_sub_block = FoliageSubBlock(
            prev_foliage_block.get_hash(),
            reward_block_hash,
            is_block,
            foliage_sub_block_data,
            plot_key_signature,
        )

        transactions_info_hash = None
        if is_transaction:
            transactions_info_hash = TransactionsInfo(
                generator_hash, final_aggsig, fees, cost, reward_claims_incorporated
            )

        foliage_block = FoliageBlock(
            prev_block_hash,
            timestamp,
            filter_hash,
            additions_root,
            removals_root,
            transactions_info_hash,
        )

        return foliage_sub_block, foliage_block, generator_hash, transactions


def get_prams_from_plots(
        self, test_constants, challenge_hash, difficulty, min_iters
) -> (ClassgroupElement, uint64, ProofOfSpace, bytes32, G2Element):
    selected_plot_info = None
    selected_proof_index = 0
    selected_quality: Optional[bytes] = None
    plots = [
        pinfo for _, pinfo in sorted(list(self.plots.items()), key=lambda x: str(x[0]))
    ]
    random.seed(seed)
    for i in range(len(plots) * 3):
        # Allow passing in seed, to create reorgs and different chains
        seeded_pn = random.randint(0, len(plots) - 1)
        plot_info = plots[seeded_pn]
        plot_id = plot_info.prover.get_id()
        ccp = ProofOfSpace.can_create_proof(
            plot_id,
            challenge_hash,
            test_constants.NUMBER_ZERO_BITS_CHALLENGE_SIG,
        )
        if not ccp:
            continue
        qualities = plot_info.prover.get_qualities_for_challenge(challenge_hash)
        if len(qualities) > 0:
            selected_plot_info = plot_info
            selected_quality = qualities[0]
            # break list.append(Tuple[selected_plot_info,selected_quality])

    # for each pos :
    assert selected_plot_info is not None
    if selected_quality is None:
        raise RuntimeError("No proofs for this challenge")

    proof_xs: bytes = selected_plot_info.prover.get_full_proof(
        challenge_hash, selected_proof_index
    )

    plot_pk = ProofOfSpace.generate_plot_public_key(
        selected_plot_info.local_sk.get_g1(),
        selected_plot_info.farmer_public_key,
    )
    proof_of_space: ProofOfSpace = ProofOfSpace(
        challenge_hash,
        selected_plot_info.pool_public_key,
        plot_pk,
        selected_plot_info.prover.get_size(),
        proof_xs,
    )

    number_iters: uint64 = pot_iterations.calculate_iterations(
        proof_of_space, difficulty, min_iters
    )

    if self.real_plots:
        print(f"Performing {number_iters} VDF iterations")

    return number_iters, proof_of_space, selected_quality, plot_pk


def get_vdf_proof(
        challenge_hash: bytes32, a: str, b: str, number_iters, discriminant_size_bits: int
) -> VDFProof:
    int_size = (discriminant_size_bits + 16) >> 4
    result = prove(challenge_hash, str(a), str(b), discriminant_size_bits, number_iters)
    output = ClassgroupElement(
        int512(
            int.from_bytes(
                result[0:int_size],
                "big",
                signed=True,
            )
        ),
        int512(
            int.from_bytes(
                result[int_size: 2 * int_size],
                "big",
                signed=True,
            )
        ),
    )

    return VDFProof(witness=output.get_hash(), witness_type=1)


# This code generates a genesis block, call as main to output genesis block to terminal
# This might take a while, using the python VDF implementation.
# Run by doing python -m tests.block_tools
if __name__ == "__main__":
    from src.util.default_root import DEFAULT_ROOT_PATH
    from src.consensus.constants import constants as consensus_constants

    initialize_logging("block_tools", {"log_stdout": True}, DEFAULT_ROOT_PATH)
    bt = BlockTools(root_path=DEFAULT_ROOT_PATH, real_plots=True)
    print(
        bytes(
            bt.create_genesis_block(
                consensus_constants,
                bytes([0] * 32),
                b"0",
                bytes32(
                    bytes.fromhex(
                        # "txch102gkhhzs60grx7cfnpng5n6rjecr89r86l5s8xux2za8k820cxsq64ssdg
                        "7a916bdc50d3d0337b0998668a4f439670339467d7e9039b8650ba7b1d4fc1a0"
                    )
                ),
            )
        )
    )
