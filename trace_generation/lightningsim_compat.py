"""Monkey-patch LightningSim for Vitis HLS 2024.2 Versal compatibility.

Vitis HLS 2024.2 adds `muxlogic` instructions to the ADB schedule for
Versal targets (e.g. VCK190) that have no LLVM IR counterpart.  This causes:

1. A strict zip mismatch in BasicBlock.instruction_llvm (extra schedule insts)
2. Inflated FIFO event counts in BasicBlock.events (extra muxlogic operands
   feed into read/write instructions as spurious INPUT edges)
3. FIFO width extraction fails because muxlogic replaces the payload operand
   on write instructions, causing all fifo_widths to be 0

This module filters muxlogic out at all affected points so the 1:1
schedule-to-LLVM mapping, correct event counts, and FIFO widths are preserved.
"""

from functools import cached_property
from typing import Dict

import llvmlite.binding as llvm
from lightningsim.model.basic_block import BasicBlock
from lightningsim.model.cdfg_edge import CDFGEdge
from lightningsim.model.instruction import Instruction

SCHEDULE_ONLY_OPCODES = {"muxlogic"}


# -- patch 1: instruction_llvm ------------------------------------------------

def _patched_instruction_llvm(self):
    def is_ignored(instruction: llvm.ValueRef):
        if instruction.opcode != "call":
            return False
        function_name = ""
        for operand in instruction.operands:
            function_name = operand.name
        return function_name.startswith("_ssdm_op_Spec")

    schedule_insts = [inst for inst in self if inst.opcode not in SCHEDULE_ONLY_OPCODES]
    instructions: Dict[Instruction, llvm.ValueRef] = dict(
        zip(
            schedule_insts,
            (instruction for instruction in self.llvm.instructions if not is_ignored(instruction)),
            strict=True,
        )
    )

    for instruction, llvm_instruction in instructions.items():
        if llvm_instruction.name:
            assert llvm_instruction.name == instruction.name, (
                f"Instruction name mismatch: {llvm_instruction.name} != {instruction.name}"
            )

    return instructions


# -- patch 2: Instruction.llvm -----------------------------------------------

def _patched_llvm_getter(self):
    return self.basic_block.instruction_llvm.get(self)


# -- patch 3: events ----------------------------------------------------------

def _patched_events(self):
    def _is_muxlogic_operand(operand):
        """Return True if this operand's source is a muxlogic instruction."""
        if operand is None:
            return False
        src = operand.source
        return hasattr(src, "opcode") and src.opcode in SCHEDULE_ONLY_OPCODES

    def event_count(instruction: Instruction):
        if instruction.opcode in SCHEDULE_ONLY_OPCODES:
            return 0

        if instruction.opcode in ("call", "readreq", "writereq", "writeresp"):
            return 1

        if instruction.opcode in ("read", "write"):
            assert instruction.llvm.opcode == "call"
            function = ""
            for operand in instruction.llvm.operands:
                function = operand.name
            function_type, interface_type, *rest = function.split(".")

            if interface_type == "m_axi":
                return 1

            if interface_type == "ap_fifo":
                if instruction.opcode == "read":
                    # For reads, muxlogic adds an extra CE operand; filter it
                    num_operands = sum(
                        int(
                            operand is not None
                            and operand.type == CDFGEdge.INPUT
                            and operand.sink_id == instruction.id
                            and not _is_muxlogic_operand(operand)
                        )
                        for operand in instruction.operands
                    )
                    return num_operands
                else:
                    # For writes, muxlogic replaces an operand; keep total
                    num_operands = sum(
                        int(
                            operand is not None
                            and operand.type == CDFGEdge.INPUT
                            and operand.sink_id == instruction.id
                        )
                        for operand in instruction.operands
                    )
                    return num_operands // 2

        return 0

    return [
        instruction for instruction in self for _ in range(event_count(instruction))
    ]

# -- patch 4: fix muxlogic bitwidth for FIFO width extraction ------------------
#
# resolve_trace extracts FIFO widths via:
#     source_instruction = event_instruction.operands[-1].source
#     fifo_widths[fifo_id] = source_instruction.bitwidth
#
# With muxlogic on Versal, source_instruction is a muxlogic Instruction whose
# ADB XML reports bitwidth=0.  The original v0.8 code (without muxlogic) got
# the width from the real payload instruction's bitwidth.
#
# Fix: monkey-patch Instruction.bitwidth so that muxlogic instructions derive
# their bitwidth from their consumer write instruction's LLVM function name
# (e.g. _ssdm_op_Write.ap_fifo.volatile.i32P0A → 32 bits).

import re

_orig_bitwidth = Instruction.bitwidth  # cached_property

_LLVM_TYPE_RE = re.compile(r"\.(?:i(\d+)|float|double|half)(?:P\d|$)")


def _width_from_llvm_name(name: str) -> int:
    """Extract data bitwidth from an SSDM LLVM function name."""
    m = _LLVM_TYPE_RE.search(name)
    if m is None:
        return 0
    if m.group(1):
        return int(m.group(1))
    token = m.group(0).lstrip(".")
    if token.startswith("float"):
        return 32
    if token.startswith("double"):
        return 64
    if token.startswith("half"):
        return 16
    return 0


@cached_property
def _patched_bitwidth(self):
    bw = _orig_bitwidth.func(self)
    if bw == 0 and self.opcode in SCHEDULE_ONLY_OPCODES:
        # Find the consumer instruction that uses this muxlogic as a source
        for edge in self.parent.edges.values():
            if edge.source_id != self.id or edge.type != CDFGEdge.INPUT:
                continue
            sink = edge.sink
            if sink is None or sink.opcode != "write":
                continue
            llvm_inst = sink.llvm
            if llvm_inst is None:
                continue
            fn = ""
            for op in llvm_inst.operands:
                fn = op.name
            if "ap_fifo" not in fn:
                continue
            w = _width_from_llvm_name(fn)
            if w > 0:
                return w
    return bw


# -- apply all patches --------------------------------------------------------

def apply_patches():
    prop = cached_property(_patched_instruction_llvm)
    prop.__set_name__(BasicBlock, "instruction_llvm")
    BasicBlock.instruction_llvm = prop

    Instruction.llvm = property(_patched_llvm_getter)

    events_prop = cached_property(_patched_events)
    events_prop.__set_name__(BasicBlock, "events")
    BasicBlock.events = events_prop

    bw_prop = cached_property(_patched_bitwidth.func)
    bw_prop.__set_name__(Instruction, "bitwidth")
    Instruction.bitwidth = bw_prop
