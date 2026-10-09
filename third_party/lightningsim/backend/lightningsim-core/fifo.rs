use std::{cmp, iter, ops};

use bincode::{Decode, Encode};
use pyo3::prelude::*;

use crate::{
    node::{NodeIndex, NodeWithDelay},
    ClockCycle,
};

pub type FifoId = u32;

const SHIFT_REGISTER_RAW_DELAY: ClockCycle = 1;
const SHIFT_REGISTER_WAR_DELAY: ClockCycle = 1;
const RAM_RAW_DELAY: ClockCycle = 2;
const RAM_WAR_DELAY: ClockCycle = 1;

#[pyclass(module = "lightningsim._core")]
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug, Decode, Encode)]
pub struct Fifo {
    #[pyo3(get)]
    pub id: FifoId,
}

pub enum FifoType {
    ShiftRegister,
    Ram,
}

#[derive(Clone, Decode, Encode)]
pub struct FifoIoNodes {
    /// Nodes which write to this FIFO.
    ///
    /// The first FIFO write node may not have any dependencies, so it may not
    /// be its own node, which is why this is a [Box] of [NodeWithDelay] instead
    /// of [NodeIndex].
    pub(crate) writes: Box<[NodeWithDelay]>,
    /// Nodes which read from this FIFO.
    ///
    /// All FIFO read nodes always have their corresponding write node as a
    /// dependency; therefore, all FIFO reads are guaranteed to be part of their
    /// own node, which is why this is a [Box] of [NodeIndex] instead of
    /// [NodeWithDelay].
    pub(crate) reads: Box<[NodeIndex]>,
}

#[pyclass(module = "lightningsim._core")]
#[derive(Clone)]
pub struct FifoIo {
    #[pyo3(get)]
    pub writes: Vec<ClockCycle>,
    #[pyo3(get)]
    pub reads: Vec<ClockCycle>,
}

impl FifoType {
    pub fn from_width_and_depth(width: u32, depth: Option<usize>) -> Self {
        match depth {
            Some(depth) => {
                let width: usize = width.try_into().unwrap();
                if depth <= 2 || width * depth <= 1024 {
                    Self::ShiftRegister
                } else {
                    Self::Ram
                }
            }
            None => Self::ShiftRegister,
        }
    }

    pub fn raw_delay(&self) -> ClockCycle {
        match self {
            Self::ShiftRegister => SHIFT_REGISTER_RAW_DELAY,
            Self::Ram => RAM_RAW_DELAY,
        }
    }

    pub fn war_delay(&self) -> ClockCycle {
        match self {
            Self::ShiftRegister => SHIFT_REGISTER_WAR_DELAY,
            Self::Ram => RAM_WAR_DELAY,
        }
    }
}

impl FifoIo {
    pub fn new<T>(fifo_io_nodes: &FifoIoNodes, node_cycles: &T) -> Self
    where
        T: ops::Index<usize, Output = ClockCycle> + ?Sized,
    {
        FifoIo {
            writes: fifo_io_nodes
                .writes
                .iter()
                .map(|node| node.resolve(node_cycles))
                .collect(),
            reads: fifo_io_nodes
                .reads
                .iter()
                .map(|&node| node_cycles[node.try_into().unwrap()])
                .collect(),
        }
    }
}

#[pymethods]
impl FifoIo {
    fn get_observed_depth(&self) -> usize {
        let mut depth: usize = 0;
        let mut max_depth: usize = 0;
        let mut write_iter = self.writes.iter();
        let mut read_iter = self.reads.iter();
        let mut next_write_cycle = write_iter.next();
        let mut next_read_cycle = read_iter.next();

        while let Some(write_cycle) = next_write_cycle {
            let read_cycle =
                next_read_cycle.expect("last read should never happen before last write");
            if write_cycle < read_cycle {
                depth += 1;
                max_depth = max_depth.max(depth);
                next_write_cycle = write_iter.next();
            } else {
                depth -= 1;
                next_read_cycle = read_iter.next();
            }
        }

        max_depth
    }
}

pub fn get_bram_count(width: u32, depth: usize) -> usize {
    if matches!(
        FifoType::from_width_and_depth(width, Some(depth)),
        FifoType::Ram
    ) {
        let mut bram_count = 0;
        let mut remaining_width: usize = width.try_into().unwrap();

        // BRAMs can be configured as 1Kx18...
        bram_count += (remaining_width / 18) * ((depth + 1023) / 1024);
        remaining_width %= 18;
        if depth <= 1024 {
            bram_count += usize::from(remaining_width != 0);
            return bram_count;
        }

        // ...and/or as 2Kx9...
        bram_count += (remaining_width / 9) * ((depth + 2047) / 2048);
        remaining_width %= 9;
        if depth <= 2048 {
            bram_count += usize::from(remaining_width != 0);
            return bram_count;
        }

        // (Ad-hoc special case found empirically)
        if depth <= 4096 && width > 18 && remaining_width == 3 {
            bram_count += 2;
            return bram_count;
        }

        // ...and/or as 4Kx4...
        bram_count += (remaining_width / 4) * ((depth + 4095) / 4096);
        remaining_width %= 4;
        if depth <= 4096 {
            bram_count += usize::from(remaining_width != 0);
            return bram_count;
        }

        // ...and/or as 8Kx2...
        bram_count += (remaining_width / 2) * ((depth + 8191) / 8192);
        remaining_width %= 2;

        // ...and/or as 16Kx1.
        bram_count += remaining_width * ((depth + 16383) / 16384);
        bram_count
    } else {
        0
    }
}

/// URAM count for a single FIFO, following Xilinx Versal URAM288 packing.
/// Each URAM288 primitive is 72 bits wide × 4096 deep.
/// HLS FIFOs use ram_depth = depth - 1 (one entry is in the output register).
pub fn get_uram_count(width: u32, depth: usize) -> usize {
    if matches!(
        FifoType::from_width_and_depth(width, Some(depth)),
        FifoType::Ram
    ) {
        let w: usize = width.try_into().unwrap();
        let ram_depth = depth - 1; // HLS FIFO: ram[0:depth-2] + output register
        let cols = (w + 71) / 72;
        let rows = (ram_depth + 4095) / 4096;
        cols * rows
    } else {
        0
    }
}

/// FF (flip-flop) count contributed by a single FIFO.
///
/// For depth <= 2 (shift-register / register-based):
///   data is stored in FFs: width × (depth - 1) for data + control overhead.
/// For depth > 2 (RAM-based):
///   only the output register (width FFs) + read/write pointer registers.
///   Pointer width = ceil(log2(depth)), with ~4 FFs per address bit.
pub fn get_ff_count(width: u32, depth: usize) -> usize {
    let w: usize = width.try_into().unwrap();
    if depth <= 1 {
        return 0;
    }
    if depth <= 2 {
        // Register-based: data stored in FFs + control logic.
        // Empirical from Vitis HLS 2024.2 csynth:
        //   16-bit FIFO depth=2: 34 FF (32 data + 2 control)
        //   128-bit FIFO depth=2: ~258 FF (256 data + 2 control)
        // Generalized: width * (depth - 1) + 2
        w * (depth - 1) + 2
    } else {
        // RAM-based: output register + pointer counters.
        // Output register = width FFs.
        // Pointer FFs = ~4 per address bit for standard FIFOs.
        let addr_bits = (depth as f64).log2().ceil() as usize;
        w + 4 * addr_bits
    }
}

/// LUT count contributed by a single FIFO.
///
/// Empirical model from Vitis HLS 2024.2 csynth regression:
///   Shallow (depth <= 2): ~8 + width/4 LUTs (mux + control)
///   Deep (depth > 2): ~4 × ceil(log2(depth)) LUTs (address decode + control)
pub fn get_lut_count(width: u32, depth: usize) -> usize {
    if depth <= 1 {
        return 0;
    }
    let w: usize = width.try_into().unwrap();
    if depth <= 2 {
        // Register-based FIFO: mux logic proportional to width.
        8 + w / 4
    } else {
        // RAM-based FIFO: address decode and control logic.
        let addr_bits = (depth as f64).log2().ceil() as usize;
        4 * addr_bits + w / 16
    }
}

pub fn get_design_space(width: u32, write_count: usize) -> impl Iterator<Item = usize> {
    fn get_step_size(width: u32) -> usize {
        if width >= 18 {
            1024
        } else if width >= 9 {
            2048
        } else if width >= 4 {
            4096
        } else if width >= 2 {
            8192
        } else {
            16384
        }
    }

    let initial_depth = 2;
    let max_depth = cmp::max(write_count, initial_depth);
    let step_size = get_step_size(width);

    let width_usize = width.try_into().unwrap();
    let max_srl_depth = 1024usize
        .checked_div(width_usize)
        .filter(|&depth| depth > initial_depth);
    let bram_start_depth =
        (max_srl_depth.unwrap_or(initial_depth) + 1).div_ceil(step_size) * step_size;

    iter::once(initial_depth)
        .chain(max_srl_depth)
        .chain((bram_start_depth..max_depth).step_by(step_size))
        .filter(move |&depth| depth < max_depth)
        .chain(iter::once(max_depth))
}
