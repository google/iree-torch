# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Script for running torchbenchmark models using torch-mlir + IREE.

Run `python torchbench.py -h` for more info.
"""
import argparse
import time
import sys
from typing import List, Optional
import torch
from torchbenchmark import load_model_by_name
import torchdynamo

import torch_mlir
import iree_torch


def _unwrap_single_tuple_return(fx_g: torch.fx.GraphModule) -> Optional[torch.fx.GraphModule]:
    """Replace tuple with tuple element in functions that return one-element tuples."""
    unwrapped_tuple = False
    for node in fx_g.graph.nodes:
        if node.op == "output":
            assert len(node.args) == 1, "Output node must have a single argument"
            node_arg = node.args[0]
            if isinstance(node_arg, tuple):
                if len(node_arg) == 1:
                    node.args = (node_arg[0],)
                    unwrapped_tuple = True
                else:
                    return None

    if not unwrapped_tuple:
        return None

    fx_g.graph.lint()
    fx_g.recompile()
    return fx_g


def torch_mlir_compiler(fx_graph: torch.fx.GraphModule,
                        example_inputs: List[torch.Tensor], use_tracing: bool):
    """Compile GraphModule using torch-mlir + IREE."""
    fx_graph_unwrapped = _unwrap_single_tuple_return(fx_graph)
    was_unwrapped = fx_graph_unwrapped is not None
    fx_graph = fx_graph_unwrapped if was_unwrapped else fx_graph
    ts_compiler = torch.jit.trace if use_tracing else torch.jit.script
    ts_graph = ts_compiler(fx_graph, example_inputs)
    linalg_module = torch_mlir.compile(ts_graph, example_inputs,
                                       output_type=torch_mlir.OutputType.LINALG_ON_TENSORS)
    compiled_module = iree_torch.compile_to_vmfb(linalg_module)
    loaded_module = iree_torch.load_vmfb(compiled_module)

    def forward(*inputs):
        result = loaded_module.forward(*inputs)
        result = tuple() if result is None else result
        return (result,) if was_unwrapped else result

    return forward


def run(func, num_iter, compiler):
    """Run a function a number of times and print out how long eveything took."""
    start_time = time.time_ns()
    for _i in range(num_iter):
        with torchdynamo.optimize(compiler):
            func()
    end_time = time.time_ns()
    print(f"Finished in {end_time - start_time} ns")


def main():
    parser = argparse.ArgumentParser(description="Run Torchbench using Torch-MLIR + IREE.")
    parser.add_argument("model", help="Model to run in torchbenchmark.")
    parser.add_argument("--train", action="store_true", help="Run model in training mode.")
    parser.add_argument("--iters", type=int, default=1,
                        help="Number of iterations to run model for.")
    parser.add_argument("--batchsize", type=int, default=0,
                        help="Batch size to use in model.")
    parser.add_argument("--trace", action="store_true", help="Use torch.jit.trace on model.")
    parser.add_argument("--exit-on-error", action="store_true", help="Exit on compiler error.")
    args = parser.parse_args()

    Model = load_model_by_name(args.model)
    if not Model:
        print(f"Model {args.model} not found in torchbench.")
        return

    test = "train" if args.train else "eval"
    model = Model(device="cpu", test=test, jit=False, batch_size=args.batchsize)
    print(f"Running model {args.model}")

    def compiler(graph, inputs):
        if args.exit_on_error:
            try:
                return torch_mlir_compiler(graph, inputs, args.trace)
            except Exception as err:
                print(err)
                sys.exit(1)
        return torch_mlir_compiler(graph, inputs, args.trace)

    run(model.invoke, args.iters, compiler)


if __name__ == "__main__":
    main()
