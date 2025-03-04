# Copyright (c) 2020-2021, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function

import os
import argparse
import timeit
import torch
import torch.cuda.nvtx as nvtx
import time

from utils.encoder import EncoderWeights, CustomEncoder
import threading

def sequence_mask(lengths, max_len=None, is_2d=True):
    batch_size = lengths.numel()
    max_len = max_len or lengths.max()
    mask = (torch.arange(0, max_len, device=lengths.device)
            .type_as(lengths)
            .repeat(batch_size, 1)
            .lt(lengths.unsqueeze(1)))
    if is_2d:
        return mask
    else:
        mask = mask.view(-1, 1, 1, max_len)
        m2 = mask.transpose(2, 3)
        return mask * m2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('batch_size', type=int,
                        help='batch size')
    parser.add_argument('layer_num', type=int,
                        help='number of layers')
    parser.add_argument('seq_len', type=int,
                        help='sequence length')
    parser.add_argument('head_num', type=int,
                        help='head number')
    parser.add_argument('head_size', type=int,
                        help='size per head')
    parser.add_argument('--size_ratio_to_full', type=int, default=1)
    parser.add_argument('--fp16', action='store_true',
                        help='is fp16')
    parser.add_argument('--int8_mode', type=int, default=0, metavar='NUMBER',
                        help='int8 mode (default: 0)', choices=[0, 1, 2, 3])
    parser.add_argument('--time', action='store_true',
                        help='test the time or not.')
    parser.add_argument('--avg_seq_len', type=int, default=-1, metavar='NUMBER',
                        help='average sequence length (default: -1)')
    parser.add_argument('--remove_padding', action='store_true',
                        help='Remove the padding of sentences of encoder.')
    parser.add_argument('--allow_gemm_test', action='store_true',
                        help='Whether allow gemm test inside FT.')
    parser.add_argument('--weight_path', type=str,
                        default=None,
                        help='path containing the pretrained weights')
    parser.add_argument('--ths_path', type=str, default='./lib/libpyt_fastertransformer.so',
                        help='path of the pyt_fastertransformer dynamic lib file')
    parser.add_argument('-thread_num', '--thread_num', type=int, default=1, metavar='int',
                        help='Testing multithread if thread_num > 1.')
    args = parser.parse_args()

    batch_size = args.batch_size
    seq_len = args.seq_len
    if args.weight_path is not None:
        if 'large' in args.weight_path:
            layer_num = 24
            head_num = 16
            head_size = 64
        elif 'base' in args.weight_path:
            layer_num = 12
            head_num = 12
            head_size = 64
        else:
            layer_num = args.layer_num
            head_num = args.head_num
            head_size = args.head_size
    else:
        layer_num = args.layer_num
        head_num = args.head_num
        head_size = args.head_size
    hidden_dim = 768 #head_num * head_size

    if args.int8_mode == 1:
        per_channel = True
    elif args.int8_mode == 2 or args.int8_mode == 3:
        per_channel = False
    elif args.int8_mode != 0:
        raise ValueError("wrong int8_mode argument")

    print("\n=============== Argument ===============")
    print('batch_size: ' + str(batch_size))
    print('layer_num: ' + str(layer_num))
    print('seq_len: ' + str(seq_len))
    print('head_num: ' + str(head_num))
    print('head_size: ' + str(head_size))
    print('hidden_dim: ' + str(hidden_dim))
    print('weight_path: ' + str(args.weight_path))
    print('use_fp16: ' + str(args.fp16))
    print('int8_mode: ' + str(args.int8_mode))
    print('avg_seq_len: ' + str(args.avg_seq_len))
    print('test_time: ' + str(args.time))
    print('remove_padding: ' + str(args.remove_padding))
    print('allow_gemm_test: ' + str(args.allow_gemm_test))
    print('ratio-to-full-bert-base-layer: ' + str(args.size_ratio_to_full))
    print("========================================\n")

    inp = torch.empty(batch_size, seq_len, hidden_dim).cuda()
    torch.nn.init.uniform_(inp, -1, 1)
    mem_seq_lens = torch.randint(1, seq_len+1, (batch_size,), dtype=torch.int32).cuda()
    if args.remove_padding:
        if args.avg_seq_len > 0:
            mem_seq_lens = torch.ones((batch_size,)) * args.avg_seq_len
            mem_seq_lens = mem_seq_lens.to(torch.int32).cuda()
        elif args.avg_seq_len == -1:
            mem_seq_lens = torch.ones((batch_size,)) * seq_len / 2
            mem_seq_lens = mem_seq_lens.to(torch.int32).cuda()
        else:
            raise ValueError("wrong avg_seq_len")

    mask = sequence_mask(mem_seq_lens, args.seq_len, False).to(torch.float)
    # mask = torch.randint(0, 2, (batch_size, seq_len, seq_len), dtype=torch.float32).cuda()
    if args.fp16 or args.int8_mode != 0:
        inp = inp.half()
        mask = mask.half()

    pretrained_weights = torch.load(args.weight_path) if (args.weight_path is not None) else None
    weights = EncoderWeights(layer_num, hidden_dim, pretrained_weights, size_ratio_to_full=args.size_ratio_to_full)

    if args.int8_mode != 0:
        weights.to_int8(per_channel, args.ths_path)
    elif args.fp16:
        weights.to_half()
    weights.to_cuda()
    custom_encoder = CustomEncoder(layer_num, head_num, head_size, weights,
                                    int8_mode=args.int8_mode,
                                    remove_padding=False, allow_gemm_test=args.allow_gemm_test,
                                    path=args.ths_path)

    eff_custom_encoder = CustomEncoder(layer_num, head_num, head_size, weights,
                                    int8_mode=args.int8_mode,
                                    remove_padding=True, allow_gemm_test=args.allow_gemm_test,
                                    path=args.ths_path)
    custom_encoder = torch.jit.script(custom_encoder)
    eff_custom_encoder = torch.jit.script(eff_custom_encoder)

    with torch.no_grad():
        output_mask = sequence_mask(mem_seq_lens, args.seq_len).to(mask.dtype).unsqueeze(-1)

        ft_output = custom_encoder(inp, mask, mem_seq_lens)[0] * output_mask
        # print(ft_output)
        print(ft_output.size())

        eff_ft_output = eff_custom_encoder(inp, mask, mem_seq_lens)[0] * output_mask
        # print(eff_ft_output)
        print(eff_ft_output.size())

        if args.time:
            iterations = 1000

            for i in range(iterations):
                output = custom_encoder(inp, mask, mem_seq_lens)
            t20 = timeit.default_timer()
            # nvtx.range_push("ext")
            for i in range(iterations):
                # nvtx.range_push("ext"+str(i))
                output = custom_encoder(inp, mask, mem_seq_lens)
                # nvtx.range_pop()
            # nvtx.range_pop()
            t2 = timeit.default_timer() - t20
            time.sleep(10)

            for i in range(iterations):
                output = eff_custom_encoder(inp, mask, mem_seq_lens)
            t30 = timeit.default_timer()
            # nvtx.range_push("eff_ext")
            for i in range(iterations):
                # nvtx.range_push("eff_ext"+str(i))
                output = eff_custom_encoder(inp, mask, mem_seq_lens)
                # nvtx.range_pop()
            # nvtx.range_pop()
            t3 = timeit.default_timer() - t30
            time.sleep(10)
            print("[INFO] FasterTransformer time costs:     {:.2f} ms".format(t2*1000/iterations))
            print("[INFO] EFF-FasterTransformer time costs: {:.2f} ms".format(t3*1000/iterations))

        if args.thread_num > 1:
            # Multi-threading demonstration
            thread_list = []
            thread_num = args.thread_num
            iterations = 100
            def run():
                t40 = timeit.default_timer()
                for i in range(iterations):
                    output = custom_encoder(inp, mask, mem_seq_lens)
                t4 = timeit.default_timer() - t40
                print("[INFO] batch_size {} max_seq_len {} {} layer FT-OP-time {:6.2f} ms with {} threads".format(batch_size,
                    seq_len, layer_num, t4, thread_num))

            for i in range(thread_num):
                thread_list.append(threading.Thread(target=run, name="RunFT"))
            for t in thread_list:
                t.start()
            for t in thread_list:
                t.join()


if __name__ == '__main__':
    main()
