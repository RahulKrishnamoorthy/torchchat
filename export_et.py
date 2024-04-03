# (c) Meta Platforms, Inc. and affiliates. Confidential and proprietary.

import time
from pathlib import Path

import torch
import torch.nn as nn
from torch.export import Dim, export

from generate import _load_model, decode_one_token
from quantize import quantize_model

from model import Transformer
# from executorch.backends.xnnpack.partition.xnnpack_partitioner import (
#    XnnpackDynamicallyQuantizedPartitioner,
#)
from executorch.backends.xnnpack.partition.xnnpack_partitioner import (
    XnnpackPartitioner,
)
from executorch.examples.portable.utils import export_to_edge

from executorch.exir.capture._config import EdgeCompileConfig, ExecutorchBackendConfig
from executorch.exir.passes.quant_fusion_pass import QuantFusionPass
from executorch.exir.passes.sym_shape_eval_pass import ConstraintBasedSymShapeEvalPass

from generate import _load_model

from model import Transformer
from torch._export import capture_pre_autograd_graph

default_device = "cpu"  # 'cuda' if torch.cuda.is_available() else 'cpu'


def device_sync(device):
    if "cuda" in device:
        torch.cuda.synchronize(device)
    elif ("cpu" in device) or ("mps" in device):
        pass
    else:
        print(f"device={device} is not yet suppported")


def materialze_broadcast_of_rope_freq_cis(
    module: torch.nn.Module,
):
    assert isinstance(module, Transformer)
    assert module.freqs_cos.dim() == 2
    dim0 = module.freqs_cos.size(0)
    dim1 = module.freqs_cos.size(1)
    assert (
        module.layers[0].attention.n_local_kv_heads
        == module.layers[0].attention.n_local_heads
    ), f"For rope freqs to be materialzed for broadcast q, k, v num heads must match. For q got {module.attention.n_kv_heads} for k got {module.attention.n_local_heads} and v got {module.attention.n_local_kv_heads}"
    num_heads = module.layers[0].attention.n_local_heads
    module.freqs_cos = module.freqs_cos.view(dim0, 1, dim1)
    module.freqs_cos = module.freqs_cos.expand(dim0, num_heads, dim1).contiguous()
    assert module.freqs_sin.dim() == 2
    assert dim0 == module.freqs_sin.size(
        0
    ), f"sin and cos freq table sizes must match. Mismatch found at dim 0: {dim0} vs {module.freqs_sin.size(0)}"
    assert dim1 == module.freqs_sin.size(
        1
    ), f"sin and cos freq table sizes must match. Mismatch found at dim 1: {dim1} vs {module.freqs_sin.size(1)}"
    module.freqs_sin = module.freqs_sin.view(dim0, 1, dim1)
    module.freqs_sin = module.freqs_sin.expand(dim0, num_heads, dim1).contiguous()
    return module


class model_wrapper(nn.Module):
    def __init__(self, model, device):
        super().__init__()

        max_seq_length = 350
        with torch.device(device):
            model.setup_caches(max_batch_size=1, max_seq_length=max_seq_length)

        self.model = model
        # init model here if necessary

    def forward(self, x, input_pos):
        # input_pos: [B, 1]
        assert input_pos.shape[-1] == 1
        logits = self.model(x, input_pos)
        return logits  # sample(logits, **sampling_kwargs)


def canonical_path(path):
    return path

## align AOTI and ET export
# def export_model(model: nn.Module, device, output_path):
def export_model(model, device, output_path, args=None) -> str:  # noqa: C901

    export_model = model_wrapper(model, device=device)
    print(export_model)

    input = (
        torch.tensor([[1]], dtype=torch.long, device=device),
        torch.tensor([0], dtype=torch.long, device=device),
    )

    state_dict = model.state_dict()
    state_dict_dtype = state_dict[next(iter(state_dict))].dtype

    # need to use kv sdpa?
    edge_config = EdgeCompileConfig(
        _check_ir_validity=False,
        _skip_type_promotion=bool(args.dtype == "fp16"),
    )

    dynamic_shapes = None

    if args.dtype is not None:
        if args.dtype == "fp16": # or args.quantization_mode == "int4":
            if state_dict_dtype != torch.float16:
                print("model.to torch.float16")
                model = model.to(dtype=torch.float16)
                state_dict_dtype = torch.float16
        elif args.dtype == "fp32":
            if state_dict_dtype != torch.float32:
                print("model.to torch.float32")
                model = model.to(dtype=torch.float32)
        else:
            raise ValueError(f"Unsupported dtype: {args.dtype}")

    with torch.nn.attention.sdpa_kernel([torch.nn.attention.SDPBackend.MATH]), torch.no_grad():
        m = capture_pre_autograd_graph(
            export_model,
            input,
            dynamic_shapes=dynamic_shapes
        )

        edge_manager = export_to_edge(
            m,
            input,
            dynamic_shapes=dynamic_shapes,
            edge_compile_config=edge_config,
        )

    edge_manager = edge_manager.to_backend(XnnpackPartitioner())
    export_program = edge_manager.to_executorch(
        ExecutorchBackendConfig(
            extract_constant_segment=True,
            extract_delegate_segments=True,
            passes=[
                QuantFusionPass(),
            ],
            sym_shape_eval_pass=ConstraintBasedSymShapeEvalPass(),
        )
    )

    print("The methods are: ", export_program.methods)
    with open(output_path, "wb") as f:
        export_program.write_to_file(f)
    # save_pte_program(export_program, output_path)

    return output_path


def main(checkpoint_path, device, output_path, args = None):
    assert checkpoint_path.is_file(), checkpoint_path

    print(f"Using device={device}")
    precision = torch.float  # bfloat16

    print("Loading model ...")
    t0 = time.time()
    model = _load_model(
        checkpoint_path, device="cpu", precision=precision, use_tp=False)

    device_sync(device=device)  # MKG
    print(f"Time to load model: {time.time() - t0:.02f} seconds")

    quantize_model(model, args.quantize)

    with torch.no_grad():
        # diverges from AOTI
        export_model(model, device, output_path, args)


def cli():
    import argparse

    parser = argparse.ArgumentParser(description="Your CLI description.")

    ######################################################################
    ### We accept these options so we can ignore them w/o error

    parser.add_argument(
        "--prompt", type=str, default="Hello, my name is", help="Input prompt."
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Whether to launch in interactive mode",
    )
    parser.add_argument("--num_samples", type=int, default=5, help="Number of samples.")
    parser.add_argument(
        "--max_new_tokens", type=int, default=200, help="Maximum number of new tokens."
    )
    parser.add_argument("--top_k", type=int, default=200, help="Top-k for sampling.")
    parser.add_argument(
        "--temperature", type=float, default=0.8, help="Temperature for sampling."
    )
    parser.add_argument(
        "--compile", action="store_true", help="Whether to compile the model."
    )
    parser.add_argument(
        "--compile_prefill",
        action="store_true",
        help="Whether to compile the prefill (improves prefill perf, but higher compile times)",
    )
    parser.add_argument("--profile", type=Path, default=None, help="Profile path.")
    parser.add_argument(
        "--speculate_k", type=int, default=5, help="Speculative execution depth."
    )
    parser.add_argument(
        "--draft_checkpoint_path",
        type=Path,
        default=None,
        help="Draft checkpoint path.",
    )
    #####################################################################

    parser.add_argument(
        "--checkpoint_path",
        type=Path,
        default="not_specified",
        help="Model checkpoint path.",
    )
    parser.add_argument(
        "-o",
        "--output-path",
        type=str,
        default="stories15M.pte",
        help="Filename"
    )
    parser.add_argument(
        "-d",
        "--dtype",
        default=None,
        help="Override the dtype of the model (default is the checkpoint dtype). Options: fp16, fp32",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--quantize",
        type=str,
        default="{ }",
        help="Quantization options."
    )


    args = parser.parse_args()
    main(args.checkpoint_path, "cpu", args.output_path, args)

if __name__ == "__main__":
    cli()