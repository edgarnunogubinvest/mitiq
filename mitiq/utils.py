# Copyright (C) 2020 Unitary Fund
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Utility functions."""
from copy import deepcopy
from typing import cast, Any, Dict, Callable, Iterable

import numpy as np

from cirq import (
    LineQubit,
    Circuit,
    CircuitDag,
    EigenGate,
    Gate,
    GateOperation,
    Moment,
    ops
    CNOT,
    H,
    DensityMatrixSimulator,
    OP_TREE,
)
from cirq.ops.measurement_gate import MeasurementGate


def _simplify_gate_exponent(gate: EigenGate) -> EigenGate:
    """Returns the input gate with a simplified exponent if possible,
    otherwise the input gate is returned without any change.

    The input gate is not mutated.

    Args:
        gate: The input gate to simplify.

    Returns: The simplified gate.
    """
    # Try to simplify the gate exponent to 1
    if hasattr(gate, "_with_exponent") and gate == gate._with_exponent(1):
        return gate._with_exponent(1)
    return gate


def _simplify_circuit_exponents(circuit: Circuit) -> None:
    """Simplifies the gate exponents of the input circuit if possible,
    mutating the input circuit.

    Args:
        circuit: The circuit to simplify.
    """
    # Iterate over moments
    for moment_idx, moment in enumerate(circuit):
        simplified_operations = []
        # Iterate over operations in moment
        for op in moment:
            assert isinstance(op, GateOperation)
            if isinstance(op.gate, EigenGate):
                simplified_gate: Gate = _simplify_gate_exponent(op.gate)
            else:
                simplified_gate = op.gate
            simplified_operation = op.with_gate(simplified_gate)
            simplified_operations.append(simplified_operation)
        # Mutate the input circuit
        circuit[moment_idx] = Moment(simplified_operations)


def _equal(
    circuit_one: Circuit,
    circuit_two: Circuit,
    require_qubit_equality: bool = False,
    require_measurement_equality: bool = False,
) -> bool:
    """Returns True if the circuits are equal, else False.

    Args:
        circuit_one: Input circuit to compare to circuit_two.
        circuit_two: Input circuit to compare to circuit_one.
        require_qubit_equality: Requires that the qubits be equal
            in the two circuits.
        require_measurement_equality: Requires that measurements are equal on
            the two circuits, meaning that measurement keys are equal.

    Note:
        If set(circuit_one.all_qubits()) = {LineQubit(0)},
        then set(circuit_two_all_qubits()) must be {LineQubit(0)},
        else the two are not equal.
        If True, the qubits of both circuits must have a well-defined ordering.
    """
    if circuit_one is circuit_two:
        return True

    circuit_one = deepcopy(circuit_one)
    circuit_two = deepcopy(circuit_two)

    if not require_qubit_equality:
        # Transform the qubits of circuit one to those of circuit two
        qubit_map = dict(
            zip(
                sorted(circuit_one.all_qubits()),
                sorted(circuit_two.all_qubits()),
            )
        )
        circuit_one = circuit_one.transform_qubits(lambda q: qubit_map[q])

    if not require_measurement_equality:
        for circ in (circuit_one, circuit_two):
            measurements = [
                (moment, op)
                for moment, op, _ in circ.findall_operations_with_gate_type(
                    MeasurementGate
                )
            ]
            circ.batch_remove(measurements)

            for i in range(len(measurements)):
                gate = cast(MeasurementGate, measurements[i][1].gate)
                gate.key = ""

            circ.batch_insert(measurements)

    return CircuitDag.from_circuit(circuit_one) == CircuitDag.from_circuit(
        circuit_two
    )


def _are_close_dict(dict_a: Dict[Any, Any], dict_b: Dict[Any, Any]) -> bool:
    """Returns True if the two dictionaries have equal keys and
    their corresponding values are "sufficiently" close."""
    keys_a = dict_a.keys()
    keys_b = dict_b.keys()
    if set(keys_a) != set(keys_b):
        return False
    for ka, va in dict_a.items():
        if not np.isclose(dict_b[ka], va):
            return False
    return True


class CircuitMismatchException(Exception):
    pass


def _generate_pmt_circuit(
        qubits: Iterable,
        depth: int,
        gate: EigenGate):
    """
    Generates a circuit which should be the identity. Given a rotation
    gate R(param), it applies R(2 * pi / depth) depth times, resulting
    in R(2*pi)
    """
    rotation_angle = 2*np.pi / depth
    moments: List[ops.Moment] = []
    for _ in range(depth):
        operations = []
        num_qubits = gate().num_qubits()
        if num_qubits != len(qubits):
            raise CircuitMismatchException(
                "Number of qubits does not match domain size of gate.")
        operations.append(gate(exponent=rotation_angle)(*qubits))
        moments.append(ops.Moment(operations))
    return Circuit(moments)


def _poor_mans_tomography(
        executor: Callable[..., float],
        gate: Gate,
        qubit: int,
        depth: int = 100) -> float:
    """
    Given an executor and a gate, determines the effective "sigma"
    that can be used for parameter noise scaling later on. 
    """

    base_gate = _get_base_gate(gate)
    circuit = _generate_pmt_circuit([qubit], depth, base_gate)
    expectation = executor(circuit)
    Q = (1 - np.power(2*expectation-1, 1/depth))/2
    sigma = -0.5*np.log(1 - 2*Q)
    return sigma

def _max_ent_state_circuit(num_qubits: int) -> Circuit:
    r"""Generates a circuits which prepares the maximally entangled state
    |\omega\rangle = U |0\rangle  = \sum_i |i\rangle \otimes |i\rangle .

    Args:
        num_qubits: The number of qubits on which the circuit is applied.
            Only 2 or 4 qubits are supported.

    Returns:
        The circuits which prepares the state |\omega\rangle.
    """

    qreg = LineQubit.range(num_qubits)
    circ = Circuit()
    if num_qubits == 2:
        circ.append(H.on(qreg[0]))
        circ.append(CNOT.on(*qreg))
    elif num_qubits == 4:
        # Prepare half of the qubits in a uniform superposition
        circ.append(H.on(qreg[0]))
        circ.append(H.on(qreg[1]))
        # Create a perfect correlation between the two halves of the qubits.
        circ.append(CNOT.on(qreg[0], qreg[2]))
        circ.append(CNOT.on(qreg[1], qreg[3]))
    else:
        raise NotImplementedError(
            "Only 2- or 4-qubit maximally entangling circuits are supported."
        )
    return circ


def _circuit_to_choi(circuit: Circuit) -> np.ndarray:
    """Returns the density matrix of the Choi state associated to the
    input circuit.

    The density matrix completely characterizes the quantum channel induced by
    the input circuit (including the effect of noise if present).

    Args:
        circuit: The input circuit.
    Returns:
        The density matrix of the Choi state associated to the input circuit.
    """
    simulator = DensityMatrixSimulator()
    num_qubits = len(circuit.all_qubits())
    # Copy and remove all operations
    full_circ = deepcopy(circuit)[0:0]
    full_circ += _max_ent_state_circuit(2 * num_qubits)
    full_circ += circuit
    return simulator.simulate(full_circ).final_density_matrix  # type: ignore


def _operation_to_choi(operation_tree: OP_TREE) -> np.ndarray:
    """Returns the density matrix of the Choi state associated to the
    input operation tree (e.g. a single operation or a sequence of operations).

    The density matrix completely characterizes the quantum channel induced by
    the input operation tree (including the effect of noise if present).

    Args:
        circuit: The input circuit.
    Returns:
        The density matrix of the Choi state associated to the input circuit.
    """
    circuit = Circuit(operation_tree)
    return _circuit_to_choi(circuit)
