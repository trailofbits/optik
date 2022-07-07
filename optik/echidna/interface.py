from maat import Cst, EVMTransaction, Value, Var, VarContext
from typing import Dict, Final, List, Optional, Tuple, Union
from ..common.exceptions import EchidnaException, GenericException
from ..common.abi import function_call
from ..common.logger import logger
from ..common.world import AbstractTx
from ..common.util import (
    twos_complement_convert,
    int_to_bool,
    echidna_parse_bytes,
    echidna_encode_bytes,
)

import os
import json
import random


# Prefix for files containing new inputs generated by the symbolic executor
NEW_INPUT_PREFIX: Final[str] = "optik_solved_input"
# Directory for temporary contract binaries to be stored for processing
TMP_CONTRACT_DIR: Final[str] = "/tmp/"


def parse_array(arr: List[Dict[str, str]]) -> Tuple[List, str]:
    """Takes a formatted array and converts it to a list of its elements

    :param arr: array of dictionaries containing types and contents

    :return: tuple containing the list of Pythonic elements, and the abi type of elements in the list
    """

    # translate each of the arguments
    el_arr = [translate_argument(el) for el in arr]

    # `translate_argument` returns type as well, so use that as the type
    arr_type = el_arr[0][0]

    # retrieve all the values
    el_arr = [el[1] for el in el_arr]

    return arr_type, el_arr


def parse_tuple(tup: List[Dict[str, str]]) -> Tuple[List, List]:
    """Takes a dynamically typed tuple and parses its values and types

    :param tup: the tuple to parse (contains ABI type representations)

    :returns tuple of ( list of types, list of values )
    """

    # translate each argument (elements are (type, value))
    el_tup = [translate_argument(el) for el in tup]

    # grab types for each element
    type_tup = [el[0] for el in el_tup]

    # extract values for each element
    el_tup = [el[1] for el in el_tup]

    return type_tup, el_tup


def translate_argument(arg: Dict) -> Tuple[str, Union[bytes, int, Value]]:
    """Translate a parsed Echidna transaction argument into a '(type, value)' tuple.
    :param arg: Transaction argument parsed as a json dict"""
    argType = arg["tag"]
    if argType == "AbiUInt":
        bits = arg["contents"][0]
        val = int(arg["contents"][1])
        return (
            f"uint{bits}",
            val,
        )
    elif argType == "AbiInt":
        bits = arg["contents"][0]
        val = int(arg["contents"][1])
        return (f"int{bits}", val)

    elif argType == "AbiAddress":
        val = int(arg["contents"], 16)
        return (
            f"address",
            val,
        )

    elif argType == "AbiBytes":
        byteLen = arg["contents"][0]
        val = echidna_parse_bytes(arg["contents"][1])
        return (
            f"bytes{byteLen}",
            val,
        )

    elif argType == "AbiBool":
        val = arg["contents"]
        return (
            f"bool",
            val,
        )

    elif argType == "AbiArray":
        num_elems = arg["contents"][0]
        array = arg["contents"][2]

        arr_type, arr = parse_array(array)

        return (f"{arr_type}[{num_elems}]", arr)

    elif argType == "AbiArrayDynamic":
        array = arg["contents"][1]

        arr_type, arr = parse_array(array)

        return (f"{arr_type}[]", arr)

    elif argType == "AbiTuple":
        contents = arg["contents"]

        types, values = parse_tuple(contents)
        type_descriptor = f"({','.join(types)})"

        return (type_descriptor, values)

    else:
        raise EchidnaException(f"Unsupported argument type: {argType}")


def load_tx(tx: Dict, tx_name: str = "") -> AbstractTx:
    """Translates a parsed echidna transaction into a Maat transaction

    :param tx: Echidna transaction parsed as a json dict
    :param tx_name: Optional name identifying this transaction, used to
        name symbolic variables created to fill the transaction data
    """
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
    logger.debug(f"Parsed arg values: {arg_values} with types: {arg_types}")
    func_signature = f"({','.join(arg_types)})"
    ctx = VarContext()
    call_data = function_call(
        func_name, func_signature, ctx, tx_name, *arg_values
    )

    # Translate block number/timestamp increments
    block_num_inc = Var(256, f"{tx_name}_block_num_inc")
    block_timestamp_inc = Var(256, f"{tx_name}_block_timestamp_inc")
    ctx.set(block_num_inc.name, int(tx["_delay"][1], 16), block_num_inc.size)
    ctx.set(
        block_timestamp_inc.name, int(tx["_delay"][0], 16), block_num_inc.size
    )

    # Translate message sender
    sender = Var(160, f"{tx_name}_sender")
    ctx.set(sender.name, int(tx["_src"], 16), sender.size)

    # Translate message value
    value = Var(256, f"{tx_name}_value")
    ctx.set(value.name, int(tx["_value"], 16), value.size)

    # Build transaction
    # TODO: make EVMTransaction accept integers as arguments
    gas_limit = Cst(256, int(tx["_gas'"], 16))
    gas_price = Cst(256, int(tx["_gasprice'"], 16))
    recipient = int(tx["_dst"], 16)
    return AbstractTx(
        EVMTransaction(
            sender,  # origin
            sender,  # sender
            recipient,  # recipient
            value,  # value
            call_data,  # data
            gas_price,  # gas price
            gas_limit,  # gas_limit
        ),
        block_num_inc,
        block_timestamp_inc,
        ctx,
    )


def load_tx_sequence(filename: str) -> List[AbstractTx]:
    """Load a sequence of transactions from an Echidna corpus file

    :param filename: corpus file to load
    """
    with open(filename, "rb") as f:
        data = json.loads(f.read())
        res = []
        for i, tx in enumerate(data):
            res.append(load_tx(tx, tx_name=f"tx{i}"))
        return res


def update_argument(arg: Dict, arg_name: str, new_model: VarContext) -> None:
    """Update an argument value in a transaction according to a
    symbolic model. The argument is modified **in-place**

    :param arg: argument to update, parsed as a JSON dict
    :param arg_name: base name of the symbolic variable that was created for this
    argument
    :param new_model: symbolic model to use to update the argument value
    """
    # Update the argument only if the model contains a new value
    # for this argument
    if all([arg_name not in var for var in new_model.contained_vars()]):
        return

    argType = arg["tag"]

    if argType == "AbiUInt":
        arg["contents"][1] = str(new_model.get(arg_name))
    elif argType == "AbiInt":
        argVal = int(new_model.get(arg_name))
        bits = arg["contents"][0]
        arg["contents"][1] = str(twos_complement_convert(argVal, bits))
    elif argType == "AbiBool":
        argVal = new_model.get(arg_name)
        arg["contents"] = int_to_bool(argVal)
    elif argType == "AbiAddress":
        arg["contents"] = str(hex(new_model.get(arg_name)))
    elif argType == "AbiBytes":
        length = arg["contents"][0]
        val = echidna_parse_bytes(arg["contents"][1])

        for i in range(length):
            byte_name = f"{arg_name}_{i}"
            if new_model.contains(byte_name):
                val[i] = new_model.get(byte_name) & 0xFF
        arg["contents"][1] = echidna_encode_bytes(val)
    elif argType == "AbiTuple":
        tuple_els = arg["contents"]

        for i, el in enumerate(tuple_els):
            sub_arg_name = f"{arg_name}_{i}"
            update_argument(el, sub_arg_name, new_model)
    elif argType == "AbiArray":
        arr_els = arg["contents"][2]

        for i, el in enumerate(arr_els):
            sub_arg_name = f"{arg_name}_{i}"
            update_argument(el, sub_arg_name, new_model)
    elif argType == "AbiArrayDynamic":
        arr_els = arg["contents"][1]

        for i, el in enumerate(arr_els):
            sub_arg_name = f"{arg_name}_{i}"
            update_argument(el, sub_arg_name, new_model)
    else:
        raise EchidnaException(f"Unsupported argument type: {argType}")


def update_tx(tx: Dict, new_model: VarContext, tx_name: str = "") -> Dict:
    """Update parameter values in a transaction according to a
    symbolic model

    :param tx: Echidna transaction to update, parsed as a JSON dict
    :param new_model: symbolic model to use to update transaction data in 'tx'
    :param tx_name: Optional name identifying the transaction to update, used to
        name get symbolci variables corresponding to the transaction data. Needs
        to match the 'tx_name' passed to load_tx() earlier
    :return: the updated transaction as a JSON dict
    """
    tx = tx.copy()  # Copy transaction to avoid in-place modifications

    # Update call arguments
    call = tx["_call"]
    args = call["contents"][1]
    for i, arg in enumerate(args):
        update_argument(arg, f"{tx_name}_arg{i}", new_model)

    # Update block number & timestamp
    block_num_inc = f"{tx_name}_block_num_inc"
    block_timestamp_inc = f"{tx_name}_block_timestamp_inc"
    if new_model.contains(block_num_inc):
        tx["_delay"][1] = hex(new_model.get(block_num_inc))
    if new_model.contains(block_timestamp_inc):
        tx["_delay"][0] = hex(new_model.get(block_timestamp_inc))

    # Update sender
    sender = f"{tx_name}_sender"
    if new_model.contains(sender):
        # Address so we need to pad it to 40 chars (20bytes)
        tx["_src"] = f"0x{new_model.get(sender):0{40}x}"

    # Update transaction value
    value = f"{tx_name}_value"
    if new_model.contains(value):
        tx["_value"] = hex(new_model.get(value))

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
    new_data = []
    for i, tx in enumerate(data):
        new_data.append(update_tx(tx, new_model, tx_name=f"tx{i}"))

    # Write new corpus input in a fresh file
    new_file = get_available_filename(
        f"{os.path.dirname(original_file)}/{NEW_INPUT_PREFIX}", ".txt"
    )
    with open(new_file, "w") as f:
        json.dump(new_data, f)


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


def extract_contract_bytecode(
    crytic_dir: str, contract_name: Optional[str]
) -> Optional[str]:
    """Parse compilation information from crytic, extracts the bytecodes
    of compiled contracts, and stores them into separate files.

    :param crytic-dir: the "crytic-export" dir created by echidna after a campaign
    :param contract_name: the name of the contract to extract
    :return: path to a file containing the bytecode for 'contract', or None on failure
    """

    def _name_from_path(path):
        return path.split(":")[-1]

    res = {}
    solc_file = str(os.path.join(crytic_dir, "combined_solc.json"))
    with open(solc_file, "rb") as f:
        data = json.loads(f.read())
        contract_key = None
        all_contracts = data["contracts"]
        all_contract_names = ",".join(iter(all_contracts))
        if contract_name is None:
            if len(all_contracts) == 1:
                contract_name = _name_from_path(next(iter(all_contracts)))
            else:
                logger.error(
                    f"Please specify the target contract among: {all_contract_names}"
                )
                return None

        for contract_path, contract_data in data["contracts"].items():
            if contract_name == _name_from_path(contract_path):
                bytecode = contract_data["bin"]
                unique_signature = hex(random.getrandbits(32))[2:]
                output_file = str(
                    os.path.join(
                        TMP_CONTRACT_DIR,
                        f"optik_contract_{unique_signature}.sol",
                    )
                )
                with open(output_file, "w") as f2:
                    logger.debug(
                        f"Bytecode for contract {contract_name} written in {output_file}"
                    )
                    f2.write(bytecode)
                return output_file

        # Didn't find contract
        logger.fatal(
            f"Couldn't find bytecode for contract {contract_name} in {solc_file}. "
            f"Available contracts: {all_contract_names}"
        )
        return None
