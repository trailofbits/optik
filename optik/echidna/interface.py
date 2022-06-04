from maat import Cst, EVMTransaction, Value, VarContext
from typing import Dict, Final, List, Tuple, Union
from ..common.exceptions import EchidnaException, GenericException
from ..common.abi import function_call
from ..common.logger import logger

import os
import json
import random

# Prefix for files containing new inputs generated by the symbolic executor
NEW_INPUT_PREFIX: Final[str] = "optik_solved_input"
# Directory for temporary contract binaries to be stored for processing
TMP_CONTRACT_DIR: Final[str] = "/tmp/"


def translate_argument(arg: Dict) -> Tuple[str, Union[bytes, int, Value]]:
    """Translate a parsed Echidna transaction argument into a '(type, value)' tuple.
    :param arg: Transaction argument parsed as a json dict"""
    if arg["tag"] == "AbiUInt":
        bits = arg["contents"][0]
        val = int(arg["contents"][1])
        return (
            f"uint{bits}",
            val,
        )
    else:
        raise EchidnaException(f"Unsupported argument type: {arg['tag']}")


def load_tx(tx: Dict) -> EVMTransaction:
    """Translates a parsed echidna transaction into a Maat transaction
    :param tx: Echidna transaction parsed as a json dict"""

    # Translate function call and argument types and values
    call = tx["_call"]
    if call["tag"] != "SolCall":
        raise EchidnaException(f"Unsupported transaction type: '{call['tag']}'")

    arg_types = []
    arg_values = []
    func_name = call["contents"][0]
    if len(call["contents"]) > 1:
        for arg in call["contents"][1]:
            t, val = translate_argument(arg)
            arg_types.append(t)
            arg_values.append(val)

    func_signature = f"({','.join(arg_types)})"
    call_data = function_call(func_name, func_signature, *arg_values)

    # Build transaction
    # TODO: correctly handle gas_limit
    # TODO: make EVMTransaction accept integers as arguments
    sender = Cst(256, int(tx["_src"], 16))
    value = Cst(256, int(tx["_value"], 16))
    gas_limit = Cst(256, 46546514651)
    recipient = int(tx["_dst"], 16)
    return EVMTransaction(
        sender,  # origin
        sender,  # sender
        recipient,  # recipient
        value,  # value
        call_data,  # data
        gas_limit,  # gas_limit
    )


def load_tx_sequence(filename: str) -> List[EVMTransaction]:
    """Load a sequence of transactions from an Echidna corpus file

    :param filename: corpus file to load
    """
    with open(filename, "rb") as f:
        data = json.loads(f.read())
        return [load_tx(tx) for tx in data]


def update_argument(arg: Dict, num: int, new_model: VarContext) -> None:
    """Update an argument value in a transaction according to a
    symbolic model. The argument is modified **in-place**

    :param arg: argument to update, parsed as a JSON dict
    :param num: position of the argument in the call. It's 0 for the 1st argument,
    1 for the 2d, etc
    :param new_model: symbolic model to use to update the argument value
    """
    if arg["tag"] == "AbiUInt":
        arg["contents"][1] = str(new_model.get(f"arg{num}"))
    else:
        raise EchidnaException(f"Unsupported argument type: {arg['tag']}")


def update_tx(tx: Dict, new_model: VarContext) -> Dict:
    """Update parameter values in a transaction according to a
    symbolic model

    :param tx: Echidna transaction to update, parsed as a JSON dict
    :param new_model: symbolic model to use to update transaction data in 'tx'
    :return: the updated transaction as a JSON dict
    """
    tx = tx.copy()  # Copy transaction to avoid in-place modifications
    call = tx["_call"]
    args = call["contents"][1]
    for i, arg in enumerate(args):
        update_argument(arg, i, new_model)

    return tx


def store_new_tx_sequence(original_file: str, new_model: VarContext) -> None:
    """Store a new sequence of transactions into a new Echidna corpus file

    :param original_file: path to the file containing the original transaction sequence
    that was replayed in order to find the new input
    :param new_model: symbolic context containing new values for the transaction data
    """
    # Load original JSON corpus input
    with open(original_file, "rb") as f:
        data = json.loads(f.read())

    # Update JSON with new transactions
    new_data = [update_tx(tx, new_model) for tx in data]

    # Write new corpus input in a fresh file
    new_file = get_available_filename(
        f"{os.path.dirname(original_file)}/{NEW_INPUT_PREFIX}", ".txt"
    )
    with open(new_file, "w") as f:
        json.dump(data, f)


def get_available_filename(prefix: str, suffix: str) -> str:
    """Get an avaialble filename. The filename will have the
    form '<prefix>_<num><suffix>' where <num> is automatically
    generated based on existing files

    :param prefix: the new file prefix, including potential absolute the path
    :param suffix: the new file suffix
    """
    num = 0
    num_max = 100000
    while os.path.exists(f"{prefix}_{num}{suffix}") and num < num_max:
        num += 1
    if num >= num_max:
        raise GenericException("Can't find available filename, very odd")
    return f"{prefix}_{num}{suffix}"


# TODO(boyan): make this support multiple files/contracts
def extract_contract_bytecode(crytic_dir: str) -> str:
    """Parse compilation information from crytic, extracts the bytecode
    of a compiled contract, and stores it into a separate file
    WARNING: currently limited to fuzzing campaigns on a single contract file!

    :param crytic-dir: the "crytic-export" dir created by echidna after a campaign
    :return: file containing the bytecode of the contract
    """
    unique_signature = hex(random.getrandbits(32))[2:]
    output_file = str(
        os.path.join(TMP_CONTRACT_DIR, f"optik_contract_{unique_signature}.sol")
    )
    with open(str(os.path.join(crytic_dir, "combined_solc.json")), "rb") as f:
        data = json.loads(f.read())
        contract_name, contract_data = next(iter(data["contracts"].items()))
        bytecode = contract_data["bin"]
        with open(output_file, "w") as f2:
            f2.write(bytecode)
    return output_file
