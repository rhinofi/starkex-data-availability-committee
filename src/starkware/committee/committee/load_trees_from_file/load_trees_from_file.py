import argparse
import asyncio
import binascii
import csv
import json
import logging
import math
import sys
from typing import List

from committee.committee import Committee
from committee.dump_trees_utils import DumpInfo, get_storage
from starkware.crypto.signature.fast_pedersen_hash import pedersen_hash_func
from starkware.starkware_utils.commitment_tree.binary_fact_tree_node import write_node_fact
from starkware.starkware_utils.commitment_tree.merkle_tree.merkle_tree import MerkleTree
from starkware.starkware_utils.commitment_tree.merkle_tree.merkle_tree_node import (
    MerkleNodeFact,
    MerkleTreeNode,
)
from starkware.starkware_utils.objects.starkex_state import (
    OrdersDict,
    OrderState,
    VaultsDict,
    VaultState,
)
from starkware.storage.storage import FactFetchingContext

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class MainHandler:
    async def main(self):
        logging.basicConfig()
        parser = argparse.ArgumentParser(
            description="Loads the committee state from csv files generated by dump_trees.py."
        )
        parser.add_argument(
            "--config_file", type=str, help="path to config file with storage configuration"
        )

        commands = {
            "vaults": self.load_vault_tree,
            "orders": self.load_order_tree,
            "info": self.load_info,
        }
        parser.add_argument("command", choices=commands.keys())

        args, command_specific_args = parser.parse_known_args()
        self.storage = await get_storage(args.config_file)
        await commands[args.command](command_specific_args)

    async def load_vault_tree(self, command_specific_args):
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--vaults_file",
            type=argparse.FileType("r"),
            required=True,
            help="Name of the input vaults csv file",
        )
        parser.add_argument(
            "--vault_height", type=int, default=31, help="Height of vaults Merkle Tree"
        )

        args = parser.parse_args(command_specific_args)

        vault_root = (await update_vaults(args.vaults_file, args.vault_height, self.storage)).root
        logger.info(f"Vault merkle root: {vault_root.hex()}")

    async def load_order_tree(self, command_specific_args):
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--orders_file",
            type=argparse.FileType("r"),
            required=True,
            help="Name of the input orders csv file",
        )
        parser.add_argument(
            "--order_height", type=int, default=251, help="Height of orders Merkle Tree"
        )
        parser.add_argument(
            "--node_idx", type=int, default=1, help="Index of the root node of the file"
        )

        args = parser.parse_args(command_specific_args)

        order_tree = await update_orders(args.orders_file, args.order_height, self.storage)
        ffc = FactFetchingContext(self.storage, pedersen_hash_func)
        subtree_root = await order_tree.get_node(ffc, args.node_idx)
        logger.info(f"Order merkle node: {subtree_root.root.hex()}")

    async def load_info(self, command_specific_args):
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--info_file",
            type=argparse.FileType("r"),
            required=True,
            help="Name of the info json file",
        )
        parser.add_argument(
            "--set_next_batch_id",
            action="store_true",
            help="Updates the state of the committee to start from the next batch",
        )

        args = parser.parse_args(command_specific_args)

        dump_info = DumpInfo.load(data=json.load(args.info_file))

        order_tree_root = await complete_tree(
            list(map(binascii.unhexlify, dump_info.order_subtree_roots)), self.storage
        )
        assert order_tree_root == dump_info.batch_info.merkle_roots["order"]

        logger.info(f"Writing batch info for batch {dump_info.batch_id}")
        await self.storage.set_value(
            Committee.committee_batch_info_key(dump_info.batch_id), dump_info.batch_info.serialize()
        )

        if args.set_next_batch_id:
            next_batch_id = dump_info.batch_id + 1
            logger.info(f"Setting next_batch_id to {next_batch_id}")
            await self.storage.set_int(Committee.next_batch_id_key(), next_batch_id)


async def update_vaults(csv_file, height, storage):
    vault_reader = csv.reader(csv_file, delimiter=",")
    vaults: VaultsDict = {}
    for row in vault_reader:
        vault_id, stark_key, token, balance = map(int, row)
        vaults[vault_id] = VaultState(stark_key=stark_key, token=token, balance=balance)

    logger.info(f"Read {len(vaults)} vaults")
    ffc = FactFetchingContext(storage, pedersen_hash_func)
    vault_tree = await MerkleTree.empty_tree(ffc, height, VaultState.empty())

    # Updating vault_tree will write the missing facts to the storage.
    vault_tree = await vault_tree.update(ffc, vaults.items())
    return vault_tree


async def update_orders(csv_file, height, storage):
    vault_reader = csv.reader(csv_file, delimiter=",")
    orders: OrdersDict = {}
    for row in vault_reader:
        order_id, fulfilled_amount = map(int, row)
        orders[order_id] = OrderState(fulfilled_amount=fulfilled_amount)

    logger.info(f"Read {len(orders)} order ids")
    ffc = FactFetchingContext(storage, pedersen_hash_func)
    order_tree = await MerkleTree.empty_tree(ffc, height, OrderState.empty())

    # Updating order_tree will write the missing facts to the storage.
    order_tree = await order_tree.update(ffc, orders.items())
    return order_tree


async def combine_nodes(
    ffc: FactFetchingContext,
    left: MerkleTreeNode,
    right: MerkleTreeNode,
) -> MerkleTreeNode:
    assert (
        left.height == right.height
    ), f"Trying to combine nodes with unequal heights ({left.height} != {right.height})."

    root_fact = MerkleNodeFact(left_node=left.root, right_node=right.root)
    root = await write_node_fact(ffc=ffc, inner_node_fact=root_fact, facts=None)
    return MerkleTreeNode(root=root, height=left.height + 1)


async def complete_tree(nodes: List[bytes], storage) -> bytes:
    """
    Gets a (full) layer of nodes in the Merkle tree, computes the top of the tree, and stores it
    in the database. For example, if nodes 4,5,6,7 are given, nodes 1,2,3 will be computed (and
    stored).
    Returns the root of the tree.
    """
    n_nodes = len(nodes)
    height = int(math.log(n_nodes, 2))
    assert 2 ** height == n_nodes, "Number of nodes must be a power of 2."

    # Note: height=0 below is incorrect, but this value is not used.
    ffc = FactFetchingContext(storage, pedersen_hash_func)
    subtrees = [MerkleTreeNode(root=node, height=0) for node in nodes]

    # Make sure all nodes are already in the database.
    for tree in subtrees:
        await tree.get_children(ffc)

    for _ in range(height):
        assert len(subtrees) % 2 == 0
        subtrees = await asyncio.gather(
            *(
                combine_nodes(ffc=ffc, left=left, right=right)
                for left, right in zip(subtrees[::2], subtrees[1::2])
            )
        )

    assert len(subtrees) == 1
    return subtrees[0].root


def run_main():
    sys.exit(asyncio.run(MainHandler().main()))
