import pathlib

from multiprocessing import freeze_support
from typing import Dict

from src.consensus.constants import ConsensusConstants
from src.consensus.default_constants import DEFAULT_CONSTANTS
from src.full_node.full_node import FullNode
from src.rpc.full_node_rpc_api import FullNodeRpcApi
from src.server.outbound_message import NodeType
from src.server.start_service import run_service
from src.util.config import load_config_cli
from src.util.default_root import DEFAULT_ROOT_PATH


# See: https://bugs.python.org/issue29288
u"".encode("idna")

SERVICE_NAME = "full_node"


def service_kwargs_for_full_node(
    root_path: pathlib.Path, config: Dict, consensus_constants: ConsensusConstants
) -> Dict:

    api = FullNode(config, root_path=root_path, consensus_constants=consensus_constants)

    kwargs = dict(
        root_path=root_path,
        api=api,
        node_type=NodeType.FULL_NODE,
        advertised_port=config["port"],
        service_name=SERVICE_NAME,
        upnp_ports=[config["port"]],
        server_listen_ports=[config["port"]],
        on_connect_callback=api._on_connect,
    )
    if config["start_rpc_server"]:
        kwargs["rpc_info"] = (FullNodeRpcApi, config["rpc_port"])
    return kwargs


def main():
    config = load_config_cli(DEFAULT_ROOT_PATH, "config.yaml", SERVICE_NAME)
    kwargs = service_kwargs_for_full_node(DEFAULT_ROOT_PATH, config, DEFAULT_CONSTANTS)
    return run_service(**kwargs)


if __name__ == "__main__":
    freeze_support()
    main()
