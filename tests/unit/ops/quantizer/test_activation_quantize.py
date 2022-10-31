"""
Copyright 2022 The Microsoft DeepSpeed Team
"""

import pytest
import torch
import deepspeed
from deepspeed.ops.op_builder import InferenceBuilder

if not deepspeed.ops.__compatible_ops__[InferenceBuilder.NAME]:
    pytest.skip("Inference ops are not available on this system",
                allow_module_level=True)

inference_module = None
torch_minor_version = None


def run_quantize_ds(activations, num_groups, q_bits, is_symmetric_quant):
    global inference_module
    if inference_module is None:
        inference_module = InferenceBuilder().load()

    return inference_module.quantize(
        activations,
        num_groups,
        q_bits,
        inference_module.Symmetric
        if is_symmetric_quant else inference_module.Asymmetric)


def get_q_props(q_bits):
    q_range = 2**q_bits
    q_min = -(2**(q_bits - 1))
    q_max = (2**(q_bits - 1) - 1)

    q_min = torch.IntTensor([q_min]).to(device='cuda')
    q_max = torch.IntTensor([q_max]).to(device='cuda')
    return q_range, q_max, q_min


def get_scale_zero_point(q_bits, is_symmetric_quant, max, min, absmax, scales = None, zero_points = None):

    q_range, q_max, q_min = get_q_props(q_bits)

    if is_symmetric_quant:
        scale = q_range / (2 * absmax)
        zero_point = torch.zeros(scale.shape, dtype=torch.int32)
    else:
        scale = q_range / (max - min)
        zero_point = q_min - (min * scale)

    return scale, zero_point


def run_ref_quantize(q_bits, is_symmetric_quant, activations_ref, num_groups):

    # Reference implementation
    # https://pytorch.org/docs/stable/quantization-support.html

    activations_ref = activations_ref.reshape(num_groups, -1).to(dtype=torch.float32)

    max_abs_activations_ref = torch.amax(torch.abs(activations_ref),
                                         dim=-1).view(num_groups,
                                                      -1)
    max_activations_ref = torch.amax(activations_ref, dim=-1).view(num_groups, -1)
    min_activations_ref = torch.amin(activations_ref, dim=-1).view(num_groups, -1)

    _, q_max, q_min = get_q_props(q_bits)

    scale, zero_point = get_scale_zero_point(q_bits, is_symmetric_quant, max_activations_ref, min_activations_ref, max_abs_activations_ref)

    data_f = activations_ref * scale

    if not is_symmetric_quant:
        data_f = data_f + zero_point

    data_i32 = torch.round(data_f).to(dtype=torch.int32)

    data_i32 = torch.minimum(torch.maximum(data_i32,
                                           q_min.expand_as(data_i32)),
                             q_max.expand_as(data_i32))
    data_i8 = data_i32.to(dtype=torch.int8)

    return data_i8, 1.0 / scale, zero_point


def int4x2to2xint4(int4X2tensor):
    high = int4X2tensor >> 4
    low = (int4X2tensor << 4) >> 4
    return torch.stack((high, low), dim=-1).flatten()


@pytest.mark.inference
@pytest.mark.parametrize("num_groups", [1, 2, 4, 8, 16, 32, 64, 512])
@pytest.mark.parametrize("num_elems", [4096, 8192, 12288, 16384])
@pytest.mark.parametrize("is_symmetric_quant", [True, False])
@pytest.mark.parametrize("q_bits", [4, 8])
def test_activation_quantize(num_elems, num_groups, is_symmetric_quant, q_bits):

    activations_ds = torch.randn((num_groups,
                                  num_elems),
                                 dtype=torch.float16,
                                 device='cuda')
    activations_ref = activations_ds.clone().detach()

    ref_out_tensor, ref_out_scales, ref_out_offsets = run_ref_quantize(q_bits, is_symmetric_quant, activations_ref, num_groups)

    ds_out_tensor, ds_out_scales, ds_out_offsets = run_quantize_ds(activations_ds, num_groups, q_bits, is_symmetric_quant)

    if (q_bits == 4):
        ds_out_tensor = int4x2to2xint4(ds_out_tensor)

    # Allow a max difference of 1 to account for differences in rounding in pytorch implementation
    assert (torch.all(
        torch.lt(torch.abs(ds_out_tensor.flatten() - ref_out_tensor.flatten()),
                 2)))
    assert (torch.allclose(ds_out_scales.flatten(), ref_out_scales.flatten()))
