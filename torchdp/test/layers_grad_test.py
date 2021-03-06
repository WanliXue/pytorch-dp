#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import unittest

import torch
import torch.nn as nn
from torchdp import PerSampleGradientClipper
from torchdp.dp_model_inspector import DPModelInspector
from torchdp.layers import DPMultiheadAttention, SequenceBias
from torchdp.utils import ConstantFlatClipper


class LayersGradTest(unittest.TestCase):
    def setUp(self):
        self.validator = DPModelInspector()

    def _reset_seeds(self):
        torch.manual_seed(1337)
        torch.cuda.manual_seed(1337)

    def _check_one_layer(self, layer, *args, **kwargs):
        if hasattr(layer, "autograd_grad_sample_hooks"):
            raise ValueError(
                f"Input layer already has hooks attached."
                f"Please provide freshly constructed layer"
            )

        self.validator.validate(layer)
        for name, param in layer.named_parameters():
            if ("weight" in name) or ("bias" in name):
                nn.init.uniform_(param, -1.0, 1.0)

        # run without DP
        self._reset_seeds()
        layer.zero_grad()
        output = layer(*args)
        if isinstance(output, tuple):
            output = output[0]
        output.norm().backward()
        vanilla_run_grads = [
            p.grad.detach().clone() for p in layer.parameters() if p.requires_grad
        ]

        # run with DP
        clipper = PerSampleGradientClipper(
            layer, ConstantFlatClipper(1999), kwargs.get('batch_first', True)
        )
        # The test outcome is sensitive to the threshold here. This test verifies that our backward
        # hooks populate sample specific gradients correctly and that when aggregated, they agree
        # with the aggregated gradients from vanilla pytorch. The clipper currently does both clipping
        # and aggregation, whereas we only want to do the latter. As a work around, we simply set a very
        # high threshold for clipping so that it effectively becomes a no-op.

        self._reset_seeds()
        layer.zero_grad()
        output = layer(*args)
        if isinstance(output, tuple):
            output = output[0]
        output.norm().backward()

        for param_name, param in layer.named_parameters():
            if param.requires_grad:
                self.assertTrue(
                    hasattr(param, "grad_sample"),
                    f"Per-sample gradients hasn't been computed for {param_name}",
                )

        clipper.clip_and_accumulate()
        clipper.pre_step()

        private_run_grads = [
            p.grad.detach().clone() for p in layer.parameters() if p.requires_grad
        ]

        # compare
        for vanilla_grad, private_grad in zip(vanilla_run_grads, private_run_grads):
            self.assertTrue(
                torch.allclose(vanilla_grad, private_grad, atol=10e-5, rtol=10e-3)
            )

    def test_conv1d(self):
        x = torch.randn(64, 16, 24)
        layer = nn.Conv1d(16, 32, 3, 1)

        self._check_one_layer(layer, x)

    def test_conv2d(self):
        x = torch.randn(64, 16, 24, 24)
        layer = nn.Conv2d(16, 32, 3, 1)

        self._check_one_layer(layer, x)

    def test_linear(self):
        self._check_one_layer(nn.Linear(8, 4), torch.randn(16, 8))
        self._check_one_layer(nn.Linear(8, 4), torch.randn(16, 8, 8))

    def test_layernorm(self):
        x = torch.randn(64, 16, 24, 24)

        self._check_one_layer(nn.LayerNorm(24), x)
        self._check_one_layer(nn.LayerNorm((24, 24)), x)
        self._check_one_layer(nn.LayerNorm((16, 24, 24)), x)

    def test_groupnorm(self):
        self._check_one_layer(nn.GroupNorm(4, 16), torch.randn(64, 16, 10))
        self._check_one_layer(nn.GroupNorm(4, 16), torch.randn(64, 16, 10, 9))
        self._check_one_layer(nn.GroupNorm(4, 16), torch.randn(64, 16, 10, 9, 8))

    def test_instancenorm(self):
        self._check_one_layer(
            nn.InstanceNorm1d(16, affine=True), torch.randn(64, 16, 10)
        )
        self._check_one_layer(
            nn.InstanceNorm2d(16, affine=True), torch.randn(64, 16, 10, 9)
        )
        self._check_one_layer(
            nn.InstanceNorm3d(16, affine=True), torch.randn(64, 16, 10, 9, 8)
        )

    def test_sequence_bias(self):
        x = torch.randn(4, 3, 2)
        layer = SequenceBias(2)

        self._check_one_layer(layer, x, batch_first=False)

    def test_multihead_attention(self):
        x = torch.randn(16, 24, 32)

        layer = DPMultiheadAttention(32, 1)
        self._check_one_layer(layer, x, x, x, batch_first=False)

        layer = DPMultiheadAttention(32, 1, bias=True, add_bias_kv=True, dropout=0.05)
        self._check_one_layer(layer, x, x, x, batch_first=False)

        layer = DPMultiheadAttention(32, 1, bias=True, add_bias_kv=True)
        self._check_one_layer(layer, x, x, x, batch_first=False)

        layer = DPMultiheadAttention(
            32, 1, bias=True, add_bias_kv=True, add_zero_attn=True
        )
        self._check_one_layer(layer, x, x, x, batch_first=False)

        q = torch.randn(16, 24, 32)
        k = torch.randn(20, 24, 28)
        v = torch.randn(20, 24, 28)
        layer = DPMultiheadAttention(
            32, 1, bias=True, add_bias_kv=True, add_zero_attn=True, kdim=28, vdim=28
        )
        self._check_one_layer(layer, q, k, v, batch_first=False)


    def test_embedding(self):
        layer = nn.Embedding(256, 100)
        x1 = torch.randint(0, 255, (128, 42)).long()
        x2 = torch.randint(0, 255, (64,)).long()
        self._check_one_layer(layer, x1)
        self._check_one_layer(layer, x2)
