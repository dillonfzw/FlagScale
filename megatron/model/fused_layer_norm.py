# Copyright (c) 2022, NVIDIA CORPORATION. All rights reserved.

"""This code is copied fron NVIDIA apex:
      https://github.com/NVIDIA/apex
   with some changes. """

import numbers
import torch
from torch.nn.parameter import Parameter
from torch.nn import init
import importlib

from megatron.core.utils import make_viewless_tensor

try:
    from apex.contrib.layer_norm.layer_norm import FastLayerNormFN
    HAVE_PERSIST_LAYER_NORM = True
except:
    HAVE_PERSIST_LAYER_NORM = False

from apex.normalization.fused_layer_norm import FusedLayerNormAffineFunction
from apex.normalization.fused_layer_norm import FusedRMSNormAffineFunction


global fused_layer_norm_cuda
fused_layer_norm_cuda = None


class MixedFusedLayerNorm(torch.nn.Module):

  def __init__(self, normalized_shape, eps=1e-5,
               no_persist_layer_norm=True,
               sequence_parallel=False,
               apply_layernorm_1p=False,
               apply_layernorm_rms=False,
               init_weight=None):
        super(MixedFusedLayerNorm, self).__init__()

        self.apply_layernorm_1p = apply_layernorm_1p
        self.apply_layernorm_rms = apply_layernorm_rms
        assert not (self.apply_layernorm_1p and self.apply_layernorm_rms), \
            "Cannot apply both 1p and rms layernorm"

        self.init_weight = init_weight
        assert self.init_weight is None or isinstance(self.init_weight, float), \
            "Cannot init_weight of None or of non-float"
        assert not (self.init_weight is not None and self.apply_layernorm_1p), \
            "Cannot float init_weight and 1p layernorm"

        global fused_layer_norm_cuda
        fused_layer_norm_cuda = importlib.import_module("fused_layer_norm_cuda")

        # List of hiddens sizes supported in the persistent layer norm kernel
        # If the hidden size is not supported, fall back to the non-persistent
        # kernel.
        persist_ln_hidden_sizes = [1024, 1536, 2048, 2304, 3072, 3840, 4096,
            5120, 6144, 8192, 10240, 12288, 12800, 15360, 16384, 18432, 20480,
            24576, 25600, 30720, 32768, 40960, 49152, 65536]
        if normalized_shape not in persist_ln_hidden_sizes or \
                not HAVE_PERSIST_LAYER_NORM:
            no_persist_layer_norm = True

        if isinstance(normalized_shape, numbers.Integral):
            normalized_shape = (normalized_shape,)
        self.normalized_shape = torch.Size(normalized_shape)
        self.eps = eps
        self.weight = Parameter(torch.Tensor(*normalized_shape))
        # no bias parameter when using rms layernorm
        if not self.apply_layernorm_rms:
            self.bias = Parameter(torch.Tensor(*normalized_shape))
        self.reset_parameters()
        self.no_persist_layer_norm = no_persist_layer_norm
        self.sequence_parallel = sequence_parallel

        # set sequence parallelism flag on weight and bias parameters
        setattr(self.weight, 'sequence_parallel', self.sequence_parallel)
        if not self.apply_layernorm_rms:
            setattr(self.bias, 'sequence_parallel', self.sequence_parallel)


  def reset_parameters(self):

    if self.apply_layernorm_1p:
        init.zeros_(self.weight)
        init.zeros_(self.bias)
    else:
        if self.init_weight:
            init.constant_(self.weight, self.init_weight)
        else:
            init.ones_(self.weight)
        if not self.apply_layernorm_rms:
            init.zeros_(self.bias)

  def forward(self, input):

    weight = self.weight + 1 if self.apply_layernorm_1p else self.weight

    if self.apply_layernorm_rms:
        return FusedRMSNormAffineFunction.apply(input, weight, self.normalized_shape, self.eps)
    elif self.no_persist_layer_norm:
        return FusedLayerNormAffineFunction.apply(input, weight, self.bias, self.normalized_shape, self.eps)
    else:
        output = FastLayerNormFN.apply(input, weight, self.bias, self.eps)

        # Apex's fast layer norm function outputs a 'view' tensor (i.e., has
        # a populated '_base' field). This will result in schedule.py's
        # deallocate_output_tensor() throwing an error, so a viewless tensor is
        # created to prevent this.
        output = make_viewless_tensor(inp = output,
                                      requires_grad = input.requires_grad,
                                      keep_graph = True)

        return output
