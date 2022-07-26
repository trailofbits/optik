from maat import (
    ARCH,
    contract,
    Cst,
    EVMContract,
    EVMTransaction,
    increment_block_number,
    increment_block_timestamp,
    Info,
    MaatEngine,
    new_evm_runtime,
    STOP,
    set_evm_bytecode,
    TX,
    TX_RES,
    Value,
    VarContext,
)
from typing import Callable, Dict, List, Optional, Union
from dataclasses import dataclass
import enum
from .exceptions import WorldException
from .util import compute_new_contract_addr


@dataclass(frozen=True)
class AbstractTx:
    """Abstract transaction. This class holds an EVMTransaction object with
    abstract transaction data, along with a VarContext that can contain
    concrete values for concolic variables present in the transacton data

    Attributes:
        tx      The full EVM transaction object
        ctx     The symbolic context associated with tx.data
    """

    tx: EVMTransaction
    block_num_inc: Value
    block_timestamp_inc: Value
    ctx: VarContext


class EVMRuntime:
    """A wrapper class for executing a single transaction in a deployed
    contract"""

    def __init__(self, engine: MaatEngine, tx: Optional[AbstractTx]):
        self.engine = engine
        if tx:
            # Load the var context of the transaction in the engine
            self.engine.vars.update_from(tx.ctx)
            # Set transaction data in contract
            contract(self.engine).transaction = tx.tx
        self.init_state = self.engine.take_snapshot()

    def run(self) -> Info:
        """Run the EVM. If the code ends in a REVERT, the state is
        not automatically reverted. See EVMRuntime.revert()
        """
        self.engine.run()
        # Return info before potential revert
        return self.engine.info

    def revert(self) -> None:
        """Revert any state modifications performed while running"""
        self.engine.restore_snapshot(self.init_state, remove=False)


class ContractRunner:
    """A wrapper class that offers an interface to deploy a contract and
    handle execution of several transactions with potential re-entrency"""

    def __init__(
        self,
        root_engine: MaatEngine,
        contract_file: str,
        address: int,
        deployer: int,
        args: List[Union[bytes, List[Value]]] = [],
        run_init_bytecode: bool = True,
    ):
        # Create a new engine that shares the variables context of the
        # root engine, but has its own memory (to hold its own bytecode)
        self.root_engine = root_engine._duplicate(share={"vars", "path"})

        # The wrapper holds a stack of pending runtimes. Each runtime represents
        # one transaction call inside the contract. The first runtime in the list
        # is the first transaction, the next ones are re-entrency calls into the
        # same contract
        self.runtime_stack: List[EVMRuntime] = []
        # Contract nonce, starts at 1 as per EIP-161
        self.nonce = 1
        self.address = address

        # Load the contract the new symbolic engine
        env = {"address": f"{address:x}", "deployer": f"{deployer:x}"}
        if not run_init_bytecode:
            # Set "no_run_init_bytecode" to anything to tell Maat to not
            # run the init bytecode
            env["no_run_init_bytecode"] = "1"

        self.root_engine.load(
            contract_file,
            args=args,
            envp=env,
        )

        # Whether the init bytecode has been run or not
        self.initialized = run_init_bytecode

    @property
    def current_runtime(self) -> EVMRuntime:
        return self.runtime_stack[-1]

    def push_runtime(self, tx: Optional[AbstractTx]) -> EVMRuntime:
        """Send a new transaction to the contract

        :param tx: The incoming transaction for which to create a new runtime
        :param is_init_runtime: True if the runtime is created
        :return: The new runtime created to execute 'tx'
        """
        # Create a new engine that shares runtime code, symbolic
        # variables, and path constraints
        new_engine = self.root_engine._duplicate(share={"mem", "vars", "path"})
        # Create new maat contract runtime for new engine
        new_evm_runtime(new_engine, self.root_engine)
        self.runtime_stack.append(EVMRuntime(new_engine, tx))
        return self.current_runtime

    def pop_runtime(self) -> None:
        """Remove the top-level runtime"""
        self.runtime_stack.pop()


class WorldMonitor:
    """Abstract interface for monitors that can execute callbacks on
    certain events"""

    def __init__(self):
        self.world: "EVMWorld" = None

    def on_attach(self, *args) -> None:
        """Callback called once when the monitor is attached to
        an EVMWorld"""
        pass

    def on_transaction(self, tx: EVMTransaction) -> None:
        """New transaction starts to be executed. This callback is triggered
        only for transactions and NOT for message calls between contracts"""
        pass

    def on_new_runtime(self, rt: EVMRuntime) -> None:
        """New EVM runtime created. This corresponds to a new transaction
        being run or execution of a message call accross contracts"""
        pass


class EVMWorld:
    """Wrapper class for deploying and running multiple contracts
    potentially interacting with each other

    Attributes:
        contracts       A dict mapping deployment addresses to a contract runner
        call_stack      A stack holding the addresses of the contracts in which
                        method calls are currently being executed. The same address
                        can appear twice in case of re-entrency
        tx_queue        A list of transactions to execute
        current_tx      Transaction currently being run
        monitors        A list of WorldMonitor that can execute callbacks on
                        various events
    """

    def __init__(self):
        self.contracts: Dict[int, ContractRunner] = {}
        self.call_stack: List[int] = []
        self.tx_queue: List[AbstractTx] = []
        self.current_tx: Optional[AbstractTx] = None
        self.monitors: List[WorldMonitor] = []
        # Counter for transactions being run
        self._current_tx_num: int = 0
        # Root engine
        self.root_engine = MaatEngine(ARCH.EVM)

    def deploy(
        self,
        contract_file: str,
        address: int,
        deployer: int,
        args: List[Union[bytes, List[Value]]] = [],
        run_init_bytecode: bool = True,
        add_to_call_stack: bool = False,
    ) -> ContractRunner:
        """Deploy a contract at a given address

        :param contract_file: compiled contract file
        :param address: address where to deploy the contract
        :param deployer: address of the account deploying the contract
        :param args: arguments to pass to contract constructor
        :param run_init_bytecode: if set to False, the contract is deployed
         but the init bytecode is not executed. A new EVMRuntime is pushed
         for the pending execution of the init bytecode.
        :param add_to_call_stack: push the newly deployed contract on the
         all stack. This means that the next frame to run will be this contract.
         Setting this parameter to True requires `run_init_bytecode` to be False
        """
        if address in self.contracts:
            raise WorldException(
                f"Couldn't deploy {contract_file}, address {address} already in use"
            )
        else:
            runner = ContractRunner(
                self.root_engine,
                contract_file,
                address,
                deployer,
                args,
                run_init_bytecode,
            )
            self.contracts[address] = runner
            return runner

    def push_transaction(self, tx: AbstractTx) -> None:
        """Add a new transaction in the transaction queue"""
        self.tx_queue.append(tx)

    def push_transactions(self, tx_list: List[AbstractTx]) -> None:
        """Add a list of transactions in the transaction queue. The transactions
        are executed in the order they have in the list"""
        for tx in tx_list:
            self.push_transaction(tx)

    def next_transaction(self) -> AbstractTx:
        """Return the next transaction to execute and remove it from the
        transaction queue"""
        res = self.tx_queue[0]
        self.tx_queue.pop(0)
        return res

    @property
    def current_tx_num(self) -> int:
        return self._current_tx_num

    @current_tx_num.setter
    def current_tx_num(self, val) -> None:
        self._current_tx_num = val

    @property
    def has_pending_transactions(self) -> bool:
        """True if the transaction queue is not empty"""
        return self.tx_queue

    @property
    def current_contract(self) -> ContractRunner:
        """Return the contract currently being executed"""
        if not self.call_stack:
            raise WorldException("No contract being currently executed")
        return self.contracts[self.call_stack[-1]]

    def get_contract(self, address: int) -> ContractRunner:
        """Return the contract deployed at 'address'"""
        if not address in self.contracts:
            raise WorldException(f"No contract deployed at {address}")
        return self.contracts[address]

    @property
    def current_engine(self) -> MaatEngine:
        """Return the MaatEngine in which code is currently being executed"""
        return self.current_contract.current_runtime.engine

    def _push_runtime(
        self, runner: ContractRunner, tx: Optional[AbstractTx]
    ) -> EVMRuntime:
        """Wrapper function to push a new runtime for a contract runner, and
        trigger the corresponding event"""
        rt = runner.push_runtime(tx)
        self._on_event("new_runtime", rt)
        return rt

    def run(self) -> STOP:
        """Run pending transactions"""

        if not (self.has_pending_transactions or self.call_stack):
            raise WorldException("No more transactions to execute")

        # Keep running as long as there are pending transactions
        # or unfinished nested message calls
        while self.has_pending_transactions or self.call_stack:
            if not self.call_stack:
                # Pop next transaction to execute
                self.current_tx = self.next_transaction()
                self.current_tx_num += 1
                # Find contract runner for the target contract
                contract_addr = self.current_tx.tx.recipient
                try:
                    runner = self.contracts[contract_addr]
                except KeyError as e:
                    raise WorldException(
                        f"Transaction recipient is {contract_addr}, but no contract is deployed there"
                    )
                # Create new runtime to run this transaction
                self._push_runtime(runner, self.current_tx)
                # Add to call stack
                self.call_stack.append(contract_addr)
                # Update block number & timestamp
                self._update_block_info(self.root_engine, self.current_tx)
                # Monitor events
                self._on_event(
                    "transaction",
                    contract(runner.current_runtime.engine).transaction,
                )

            # Get current runtime and run
            rt: EVMRuntime = self.current_contract.current_runtime
            info = rt.run()
            stop = info.stop
            # Check stop reason
            if stop == STOP.EXIT:
                succeeded: bool = info.exit_status.as_uint() in [
                    TX_RES.STOP,
                    TX_RES.RETURN,
                ]

                # Set transaction result in potential caller contract
                is_msg_call_return = False
                if len(self.call_stack) >= 2:
                    caller = contract(
                        self.contracts[
                            self.call_stack[-2]
                        ].current_runtime.engine
                    )
                    caller.result_from_last_call = contract(
                        rt.engine
                    ).transaction.result
                    is_msg_call_return = True
                # Handle revert. WARNING: Once we revert the state, 'info' is
                # no more valid, because it is restored as well
                # Note: doing exit_status.as_uint() is safe here because
                # exit_status will never be symbolic for the EVM architecture
                if info.exit_status.as_uint() == TX_RES.REVERT:
                    rt.revert()

                # If contract was still initializing, handle success/failure
                if not self.current_contract.initialized:
                    # This pushes the success status in the caller contract
                    # stack, and deletes the contract runner for current_contract
                    # if the contract creation failed
                    self._handle_CREATE_after(succeeded)

                # Delete the runtime
                self.current_contract.pop_runtime()

                # Handle call result in potential caller contract
                if is_msg_call_return:
                    caller_contract = contract(
                        self.contracts[
                            self.call_stack[-2]
                        ].current_runtime.engine
                    )
                    # Handle return of CALL/DELEGATECALL/CALLCODE
                    if caller_contract.outgoing_transaction.type in [
                        TX.CALL,
                        TX.CALLCODE,
                        TX.DELEGATECALL,
                    ]:
                        self._handle_CALL_after(caller_contract, succeeded)

                    # Reset outgoing_transaction in caller
                    caller_contract.outgoing_transaction = None

                # Remove current contract from callstack
                self.call_stack.pop()

            elif stop == STOP.NONE and contract(rt.engine).outgoing_transaction:
                out_tx = contract(rt.engine).outgoing_transaction
                # Increment global transaction count
                self.current_tx_num += 1
                # Handle message call
                if out_tx.type in [TX.CREATE, TX.CREATE2]:
                    self._handle_CREATE()
                elif out_tx.type == TX.CALL:
                    self._handle_CALL()
                # TODO(boyan): other tx types, CALLCODE, DELEGATECALL, ...
                else:
                    raise WorldException(
                        "Contract emitted an unsupported transaction type"
                    )

            # Any other return reason: event hook, error, ... -> we stop
            else:
                break

        return stop

    def _handle_CREATE(self) -> None:
        """Handle deployment of a new contract by another contract with
        the CREATE or CREATE2 EVM instructions. This method deploys the new
        contract without running the init bytecode, and pushes it at the top
        of the call stack, so it will be the next contract to run."""

        rt: EVMRuntime = self.current_contract.current_runtime
        out_tx = contract(rt.engine).outgoing_transaction
        deployer = out_tx.sender.as_uint(rt.engine.vars)

        # Get address of new contract
        if out_tx.type == TX.CREATE:
            new_contract_addr = compute_new_contract_addr(
                deployer, self.current_contract.nonce
            )
        else:
            # TODO(boyan): support CREATE2
            raise WorldException(
                f"Transaction type {out_tx.type} not implemented"
            )
        # Increment caller nonce
        self.current_contract.nonce += 1

        # Deploy contract without running the init bytecode
        contract_runner = self.deploy(
            "",  # No file, bytecode is in the tx data
            new_contract_addr,
            deployer,
            args=[out_tx.data],
            run_init_bytecode=False,
        )

        # Create a new runtime for the new contract because its init
        # code must run next
        create_tx = AbstractTx(
            out_tx.deepcopy(),
            self.current_tx.block_num_inc,
            self.current_tx.block_timestamp_inc,
            VarContext(),
        )
        new_rt: EVMRuntime = self._push_runtime(contract_runner, create_tx)
        self.call_stack.append(new_contract_addr)

    def _handle_CREATE_after(self, succeeded: bool) -> None:
        """Handles returning from a CREATE message call. In case of
        failure, the contract runner for the contract being created
        is deleted.

        This method pushes either the new contract address, or zero (on
        failure), to the caller runtime stack.

        Prerequisites:
            - the current_contract MUST be the newly created contract
            - the call stack must contain at least one contract before
            the current contract (so minimum 2 contracts in total)
        """
        current_engine = self.current_contract.current_runtime.engine
        if succeeded:
            self.current_contract.initialized = True
            create_result = self.current_contract.address
            # Overwrite init bytecode with runtime code
            set_evm_bytecode(
                current_engine,
                contract(current_engine).transaction.result.return_data,
            )
        else:
            # if CREATE failed, remove the contract runner for the
            # new contract
            del self.contracts[self.current_contract.address]
            create_result = 0
        # Push new contract address in caller contract if any
        if len(self.call_stack) > 1:
            contract(
                self.contracts[self.call_stack[-2]].current_runtime.engine
            ).stack.push(Cst(256, create_result))

    def _handle_CALL(self) -> None:
        """Handles message call into another contract. This method
        creates a new transaction and a new runtime for the target
        contract, and pushes it at the top of the call stack, so it
        will be the next contract to run"""

        rt: EVMRuntime = self.current_contract.current_runtime
        out_tx = contract(rt.engine).outgoing_transaction
        # Get runner for target contract
        try:
            contract_runner = self.contracts[out_tx.recipient]
        except KeyError:
            raise WorldException(
                f"Contract emitted call to {out_tx.recipient:x} "
                "but no contract deployed at this address"
            )

        tx = AbstractTx(
            out_tx.deepcopy(),
            self.current_tx.block_num_inc,
            self.current_tx.block_timestamp_inc,
            VarContext(),
        )
        self._push_runtime(contract_runner, tx)
        self.call_stack.append(contract_runner.address)

    def _handle_CALL_after(
        self, caller_contract: EVMContract, succeeded: bool
    ) -> None:
        """Handles return of a message call into a contract by
        pushing the success status in the caller contract's stack"""
        # Push the success status of transaction in caller stack
        # if the terminating runtime was called with
        caller_contract.stack.push(Cst(256, 1 if succeeded else 0))
        # Write the return data in memory
        if (
            caller_contract.outgoing_transaction.ret_len.as_uint()
            < caller_contract.result_from_last_call.return_data_size
        ):
            raise WorldException(
                "Message call returned more bytes than the buffer-size allocated by the caller contract"
            )
        caller_contract.memory.write_buffer(
            caller_contract.outgoing_transaction.ret_offset,
            caller_contract.result_from_last_call.return_data,
        )

    def _update_block_info(self, m: MaatEngine, tx: AbstractTx) -> None:
        """Increase the block number and block timestamp when emulating
        a new transaction

        :param m: any MaatEngine running in the Ethereum environment
        for which we want to update the block info
        :param tx: transaction data containing the block info increments
        """
        # Update block info in Maat
        increment_block_number(m, tx.block_num_inc)
        increment_block_timestamp(m, tx.block_timestamp_inc)
        # TODO(boyan): Should we add constraints to force time increments to
        # be within certain bounds?

    def attach_monitor(self, monitor: WorldMonitor, *args) -> None:
        """Attach a WorldMonitor"""
        if monitor in self.monitors:
            raise WorldException("Monitor already attached")
        self.monitors.append(monitor)
        monitor.world = self
        monitor.on_attach(*args)

    def detach_monitor(self, monitor: WorldMonitor) -> None:
        """Detach a WorldMonitor"""
        if monitor not in self.monitors:
            raise WorldException("Monitor was not attached")
        self.monitors.remove(monitor)

    def _on_event(self, event_name: str, *args) -> None:
        for m in self.monitors:
            callback = getattr(m, f"on_{event_name}")
            callback(*args)
